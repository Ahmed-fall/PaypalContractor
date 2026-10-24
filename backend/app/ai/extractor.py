"""LLM structured contract-rule extraction with a deterministic fallback.

Chain: Gemini primary -> Gemini backup model -> regex parser, inside one 8-second budget.
Each model gets one quick retry on a transient error (429/5xx). Only one API key is needed
(GEMINI_API_KEY); without it the regex parser alone handles the house contract style.
The model only copies parameters and verbatim quotes; every answer is checked against
the source text (quotes verbatim, amounts and due dates present in their own clause)
before it is accepted. Confidence is assigned here, never self-reported by a model.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError

from ..schemas.contracts import ContractExtractionResult, ExtractedMilestoneSpec
from .llm import (
    DEFAULT_GEMINI_FALLBACK_MODEL,
    DEFAULT_GEMINI_MODEL,
    MIN_PROVIDER_SLOT_SECONDS,
    TRANSIENT_RETRY_DELAY_SECONDS,
    GeminiModel,
    configured_key,
    gemini_models_from_env,
    is_transient_error,
    run_with_failover,
)
from .pdf_parser import ContractParseError, extract_text_from_upload
from .prompts import EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt

EXTRACTION_BUDGET_SECONDS = 8.0
PROVIDER_TIMEOUT_SECONDS = 6.0
LLM_CONFIDENCE = 0.95
REGEX_CONFIDENCE = 0.80

_CENT = Decimal("0.01")
_MIN_QUOTE_CHARS = 12


class ExtractionValidationError(ValueError):
    """A candidate extraction that is malformed or not supported by the source text."""


# --- model-facing schema (strings only: no floats, no provider-specific types) ----------


class LLMMilestone(BaseModel):
    milestone_code: str = Field(description='Milestone code as written, e.g. "MS-01"')
    title: str = Field(description="Short milestone name as written in the contract")
    expected_amount: str = Field(
        description='Amount as a plain decimal, e.g. "1000.00"'
    )
    due_date: str = Field(description="Due date as YYYY-MM-DD")
    source_clause_quote: str = Field(
        description="Verbatim sentence stating this payment"
    )


class LLMContract(BaseModel):
    contract_title: str
    client_name: str
    client_paypal_email: str
    currency: str = Field(description='ISO-4217 code, e.g. "USD"')
    late_fee_percentage: str = Field(
        description='Late-fee percent as a plain decimal, e.g. "5.00"'
    )
    grace_period_days: int
    allow_partial_payments: bool
    penalty_clause_quote: str = Field(
        description="Verbatim late-fee sentence, or empty"
    )
    milestones: list[LLMMilestone]


# --- providers --------------------------------------------------------------------------


class ContractLLMProvider(Protocol):
    name: str

    async def extract(
        self, system_prompt: str, user_prompt: str, timeout: float
    ) -> LLMContract: ...


class GeminiProvider(GeminiModel):
    async def extract(
        self, system_prompt: str, user_prompt: str, timeout: float
    ) -> LLMContract:
        return await self.generate_json(system_prompt, user_prompt, LLMContract)


def default_providers() -> list[ContractLLMProvider]:
    """Gemini models configured from the environment, in failover order."""
    key = configured_key(os.environ.get("GEMINI_API_KEY"))
    return (
        [GeminiProvider(key, model) for model in gemini_models_from_env()]
        if key
        else []
    )


# --- conversion to the canonical contract --------------------------------------------------

_PLAIN_NUMBER = re.compile(r"^\d+(?:\.\d{1,2})?$")


def _parse_decimal(raw: str, field: str, *, strip: str) -> Decimal:
    cleaned = raw.strip()
    for ch in strip:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    if not _PLAIN_NUMBER.fullmatch(cleaned):
        raise ExtractionValidationError(f"{field} is not a plain number: {raw!r}")
    return Decimal(cleaned).quantize(_CENT, rounding=ROUND_HALF_UP)


def to_extraction_result(
    answer: LLMContract, confidence: float
) -> ContractExtractionResult:
    try:
        milestones = [
            ExtractedMilestoneSpec(
                milestone_code=m.milestone_code.strip(),
                title=m.title.strip(),
                expected_amount=_parse_decimal(
                    m.expected_amount, "expected_amount", strip="$,"
                ),
                due_date=date.fromisoformat(m.due_date.strip()),
                source_clause_quote=m.source_clause_quote.strip(),
            )
            for m in answer.milestones
        ]
        return ContractExtractionResult(
            contract_title=answer.contract_title.strip(),
            client_name=answer.client_name.strip(),
            client_paypal_email=answer.client_paypal_email.strip(),
            currency=answer.currency.strip().upper(),
            late_fee_percentage=_parse_decimal(
                answer.late_fee_percentage, "late_fee_percentage", strip="%"
            ),
            grace_period_days=answer.grace_period_days,
            allow_partial_payments=answer.allow_partial_payments,
            penalty_clause_quote=answer.penalty_clause_quote.strip(),
            milestones=milestones,
            extraction_confidence=confidence,
        )
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ExtractionValidationError):
            raise
        raise ExtractionValidationError(
            f"answer does not fit the contract schema: {exc}"
        ) from exc


# --- verification against the source text ------------------------------------------------

_TYPOGRAPHY = str.maketrans(
    {
        "—": "-",
        "–": "-",
        "‒": "-",
        "−": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "\xa0": " ",
        "•": " ",
        "*": None,
        "`": None,
    }
)
_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?(?!\d)")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)")
_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_MDY = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.IGNORECASE
)
_DATE_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\.?,?\s+(\d{{4}})", re.IGNORECASE
)
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def flatten(text: str) -> str:
    """Typography-normalised, single-spaced text (case preserved)."""
    return _WHITESPACE.sub(" ", text.translate(_TYPOGRAPHY)).strip()


def _canon(text: str) -> str:
    return flatten(text).casefold()


def _numbers_in(text: str) -> set[Decimal]:
    return {
        Decimal(whole.replace(",", "") + (frac or ""))
        for whole, frac in _NUMBER.findall(text)
    }


def _percents_in(text: str) -> set[Decimal]:
    return {Decimal(p) for p in _PERCENT.findall(text)}


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def dates_in(text: str) -> set[date]:
    found: set[date | None] = set()
    for mon, day, year in _DATE_MDY.findall(text):
        found.add(_safe_date(int(year), _MONTHS[mon.lower()], int(day)))
    for day, mon, year in _DATE_DMY.findall(text):
        found.add(_safe_date(int(year), _MONTHS[mon.lower()], int(day)))
    for year, month, day in _DATE_ISO.findall(text):
        found.add(_safe_date(int(year), int(month), int(day)))
    return {d for d in found if d is not None}


def _is_verbatim(quote: str, source: str) -> bool:
    q = _canon(quote).rstrip(" .;:")
    return len(q) >= _MIN_QUOTE_CHARS and q in source


def verify_against_source(result: ContractExtractionResult, text: str) -> None:
    """Raise ``ExtractionValidationError`` unless every extracted fact is grounded in ``text``."""
    source = _canon(text)
    problems: list[str] = []

    if not result.milestones:
        problems.append("no milestones")
    codes = [m.milestone_code for m in result.milestones]
    if len(set(codes)) != len(codes):
        problems.append("duplicate milestone codes")

    for m in result.milestones:
        quote = flatten(m.source_clause_quote)
        if not _is_verbatim(quote, source):
            problems.append(f"{m.milestone_code}: quote is not verbatim")
        if m.expected_amount <= 0 or m.expected_amount not in _numbers_in(quote):
            problems.append(f"{m.milestone_code}: amount not stated in its clause")
        if m.due_date not in dates_in(quote):
            problems.append(f"{m.milestone_code}: due date not stated in its clause")

    if result.penalty_clause_quote and not _is_verbatim(
        result.penalty_clause_quote, source
    ):
        problems.append("penalty quote is not verbatim")
    if (
        result.late_fee_percentage > 0
        and result.late_fee_percentage
        not in _percents_in(flatten(result.penalty_clause_quote))
    ):
        problems.append("late fee not stated in the penalty clause")

    if str(result.client_paypal_email).casefold() not in source:
        problems.append("payer email not found in contract")

    if problems:
        raise ExtractionValidationError("; ".join(problems))


# --- deterministic regex fallback ---------------------------------------------------------

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"])|\s+(?=Section \d)|\s+(?=•)")
_CODE = re.compile(r"\bMS-\d+\b")
_DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TITLE_PATTERNS = (
    re.compile(r'MS-\d+\s*-\s*"([^"]+)"'),
    re.compile(r"\(MS-\d+\):\s*(.+?)\s+-\s+\$"),
    re.compile(
        r"delivery of (?:the )?(.+?)\s*\((?:Milestone Code )?MS-\d+\)", re.IGNORECASE
    ),
)
_PARTIAL_DENY = (
    "forbidden",
    "may not",
    "not permitted",
    "prohibited",
    "not allowed",
    "shall not",
)
_PARTIAL_ALLOW = ("permitted", "allowed", "may be paid", "may pay")


def _sentences(text: str) -> list[str]:
    lines = [ln.strip().lstrip("#").strip() for ln in text.splitlines()]
    flat = flatten("\n".join(lines))
    return [s.strip() for s in _SENTENCE_BREAK.split(flat) if s.strip()]


def _first(pattern: re.Pattern[str], text: str, group: int = 1) -> str | None:
    match = pattern.search(text)
    return match.group(group) if match else None


def _milestone_title(sentence: str, code: str) -> str:
    for pattern in _TITLE_PATTERNS:
        title = _first(pattern, sentence)
        if title:
            return title.strip()
    return f"Milestone {code}"


def regex_extract(text: str) -> LLMContract:
    """Rule-based extraction for the house contract style. Never calls a model."""
    sentences = _sentences(text)
    flat = flatten(text)

    heading = next(
        (ln.strip().lstrip("#").strip() for ln in text.splitlines() if ln.strip()),
        "Contract",
    )
    client = _first(re.compile(r'\band\s+(.+?)\s+\("Client"\)'), flat) or ""
    payer = re.search(
        r"payer address[^@]*?(" + _EMAIL.pattern + ")", flat, re.IGNORECASE
    )
    email = payer.group(1) if payer else (_first(_EMAIL, flat, 0) or "")
    currency = _first(re.compile(r"Currency:[^.]*?\(([A-Z]{3})\)"), flat) or "USD"

    milestones: list[LLMMilestone] = []
    seen: set[str] = set()
    for sentence in sentences:
        quote = sentence.lstrip("• ").strip()
        code = _first(_CODE, quote, 0)
        amount = _DOLLARS.search(quote)
        due = sorted(dates_in(quote))
        if not code or not amount or not due or code in seen:
            continue
        seen.add(code)
        milestones.append(
            LLMMilestone(
                milestone_code=code,
                title=_milestone_title(quote, code),
                expected_amount=amount.group(1).replace(",", "")
                + (amount.group(2) or ""),
                due_date=due[0].isoformat(),
                source_clause_quote=quote,
            )
        )

    penalty = next(
        (s for s in sentences if "late fee" in s.casefold() and _PERCENT.search(s)), ""
    )
    fee = _first(_PERCENT, penalty) or "0.00"

    lowered = flat.casefold()
    grace = 0
    if "no grace period" not in lowered:
        found = _first(re.compile(r"\((\d+)\)\s*calendar days"), lowered) or _first(
            re.compile(r"more than (\d+) (?:calendar |business )?days past"), lowered
        )
        grace = int(found) if found else 0

    allow_partial = False
    for sentence in sentences:
        s = sentence.casefold()
        if not any(k in s for k in ("partial payment", "instalment", "installment")):
            continue
        if any(k in s for k in _PARTIAL_DENY):
            allow_partial = False
            break
        if any(k in s for k in _PARTIAL_ALLOW):
            allow_partial = True
            break

    if not milestones:
        raise ExtractionValidationError(
            "no milestone with an amount and due date was found"
        )

    title_main = heading.translate(_TYPOGRAPHY).split(" - ")[0].strip()
    return LLMContract(
        contract_title=f"{title_main} - {client}" if client else title_main,
        client_name=client,
        client_paypal_email=email,
        currency=currency,
        late_fee_percentage=fee,
        grace_period_days=grace,
        allow_partial_payments=allow_partial,
        penalty_clause_quote=penalty,
        milestones=milestones,
    )


# --- orchestration ------------------------------------------------------------------------


async def extract_rules_from_text(
    text: str,
    *,
    providers: Sequence[ContractLLMProvider] | None = None,
    budget_seconds: float = EXTRACTION_BUDGET_SECONDS,
    provider_timeout: float = PROVIDER_TIMEOUT_SECONDS,
    min_slot: float = MIN_PROVIDER_SLOT_SECONDS,
    retry_delay: float = TRANSIENT_RETRY_DELAY_SECONDS,
) -> ContractExtractionResult:
    chain = default_providers() if providers is None else list(providers)
    user_prompt = build_extraction_prompt(text)

    async def attempt(
        provider: ContractLLMProvider, timeout: float
    ) -> ContractExtractionResult:
        answer = await provider.extract(EXTRACTION_SYSTEM_PROMPT, user_prompt, timeout)
        result = to_extraction_result(answer, LLM_CONFIDENCE)
        verify_against_source(result, text)
        return result

    found = await run_with_failover(
        chain,
        attempt,
        purpose="extraction",
        budget_seconds=budget_seconds,
        provider_timeout=provider_timeout,
        min_slot=min_slot,
        retry_delay=retry_delay,
    )
    if found is not None:
        return found

    try:
        result = to_extraction_result(regex_extract(text), REGEX_CONFIDENCE)
        verify_against_source(result, text)
    except ExtractionValidationError as exc:
        raise ContractParseError(
            "no_terms",
            "No payment milestone with an amount and due date could be found in the contract.",
        ) from exc
    return result


async def extract_contract_rules(
    file_bytes: bytes,
    filename: str,
    *,
    providers: Sequence[ContractLLMProvider] | None = None,
) -> ContractExtractionResult:
    """Public entry point: uploaded bytes -> verified ``ContractExtractionResult``."""
    text = extract_text_from_upload(file_bytes, filename)
    return await extract_rules_from_text(text, providers=providers)


__all__ = [
    "DEFAULT_GEMINI_FALLBACK_MODEL",
    "DEFAULT_GEMINI_MODEL",
    "ContractLLMProvider",
    "ExtractionValidationError",
    "GeminiProvider",
    "LLMContract",
    "default_providers",
    "extract_contract_rules",
    "extract_rules_from_text",
    "is_transient_error",
    "regex_extract",
    "to_extraction_result",
    "verify_against_source",
]

"""Hybrid compliance evaluation: deterministic money, model-written explanation.

``evaluate_payment_compliance`` runs ``compliance_math`` first; those figures are final.
A Gemini model may then write the audit summary and invoice note from the locked figures,
but its text is only accepted if every amount it states is one of ours and the total due
is present. The invoice note always ends with an engine-written footer (verbatim clause and
exact amount due), and the line items come from the math alone. Any model failure falls
back to a deterministic template, so the result is always valid and correct.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field

from ..schemas.contracts import (
    ContractActivateRequest,
    ContractExtractionResult,
    DiscrepancyEvaluationResult,
    DiscrepancyType,
    ExtractedMilestoneSpec,
    InvoiceLineItemSpec,
    PersistedContractExtraction,
)
from .compliance_math import DiscrepancyMath, calculate_discrepancy_math
from .llm import (
    MIN_PROVIDER_SLOT_SECONDS,
    TRANSIENT_RETRY_DELAY_SECONDS,
    GeminiModel,
    configured_key,
    gemini_models_from_env,
    run_with_failover,
)
from .prompts import AUDIT_SYSTEM_PROMPT, build_audit_prompt

# Runs synchronously on /capture, so it bounds user-visible latency. Only the prose is at
# stake (money is already final), so we wait at most 3 s before using the template.
AUDIT_BUDGET_SECONDS = 3.0
AUDIT_PROVIDER_TIMEOUT_SECONDS = 2.5
SUMMARY_MAX_CHARS = 600
INVOICE_NOTE_MAX_CHARS = 1500
ITEM_NAME_MAX_CHARS = 120
ITEM_DESCRIPTION_MAX_CHARS = 500
_CLAUSE_IN_FOOTER_MAX_CHARS = 600
# The engine-written invoice-note footer always starts with one of these lines. Single
# source of truth: paypal_tools.engine_note_footer locates the footer with them.
FOOTER_CLAUSE_PREFIX = "Contract reference: "
FOOTER_AMOUNT_PREFIX = "Amount due: "

# Any of the backend's contract shapes works: the AI result, the persisted one, or the
# human-edited activation payload.
ContractTerms = (
    ContractExtractionResult | PersistedContractExtraction | ContractActivateRequest
)


class AuditProse(BaseModel):
    ai_audit_summary: str = Field(description="1-3 plain sentences, max 400 characters")
    paypal_invoice_detail_note: str = Field(
        description="2-4 plain sentences, max 900 characters"
    )


class ProseRejected(ValueError):
    """Model-written text that states figures we did not compute, or breaks limits."""


# --- writers ------------------------------------------------------------------------------


class AuditProseWriter(Protocol):
    name: str

    async def write_audit(
        self, system_prompt: str, user_prompt: str, timeout: float
    ) -> AuditProse: ...


class GeminiAuditWriter(GeminiModel):
    async def write_audit(
        self, system_prompt: str, user_prompt: str, timeout: float
    ) -> AuditProse:
        return await self.generate_json(system_prompt, user_prompt, AuditProse)


def default_writers() -> list[AuditProseWriter]:
    key = configured_key(os.environ.get("GEMINI_API_KEY"))
    return (
        [GeminiAuditWriter(key, model) for model in gemini_models_from_env()]
        if key
        else []
    )


# --- formatting -----------------------------------------------------------------------------


def format_money(amount: Decimal, currency: str) -> str:
    figure = f"{amount:,.2f}"
    return f"${figure}" if currency == "USD" else f"{currency} {figure}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) if "\n" not in text else text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + "…"


_SECTION = re.compile(
    r"\s*(Section\s+\d+(?:\.\d+)*(?:\s*\([^)]{1,40}\))?)", re.IGNORECASE
)


def section_ref(clause: str) -> str:
    match = _SECTION.match(clause)
    return match.group(1) if match else "the contract"


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


@dataclass(frozen=True)
class _Facts:
    """Every human-facing figure, formatted once from the locked math."""

    math: DiscrepancyMath
    currency: str
    captured_currency: str
    code: str
    title: str
    contract_title: str
    client_name: str
    expected: str
    captured: str
    shortfall: str
    penalty: str
    total: str
    fee_pct: str
    due: str
    captured_on: str
    days_after_due: int
    grace_days: int
    partial_allowed: bool
    clause: str

    @property
    def section(self) -> str:
        return section_ref(self.clause)

    def as_prompt_facts(self, lines: Sequence[InvoiceLineItemSpec]) -> dict[str, str]:
        return {
            "contract_title": self.contract_title,
            "client_name": self.client_name,
            "milestone_code": self.code,
            "milestone_title": self.title,
            "discrepancy_type": self.math.discrepancy_type.value,
            "contract_currency": self.currency,
            "expected_amount": self.expected,
            "captured_amount": self.captured,
            "captured_currency": self.captured_currency,
            "shortfall_amount": self.shortfall,
            "penalty_amount": self.penalty,
            "total_remediation_amount": self.total,
            "late_fee_percentage": self.fee_pct,
            "due_date": self.due,
            "captured_date": self.captured_on,
            "days_after_due_date": str(self.days_after_due),
            "grace_period_days": str(self.grace_days),
            "partial_payments_allowed": "yes" if self.partial_allowed else "no",
            "invoice_lines": "; ".join(
                f"{item.name}: {format_money(item.unit_amount_value, self.currency)}"
                for item in lines
            )
            or "none",
        }


def _violated_clause(
    math: DiscrepancyMath, contract: ContractTerms, ms: ExtractedMilestoneSpec
) -> str:
    if math.is_compliant:
        return ""
    late = math.discrepancy_type in (
        DiscrepancyType.LATE_PAYMENT,
        DiscrepancyType.UNDERPAID_AND_LATE,
    )
    if math.penalty_amount > 0 or late:
        return contract.penalty_clause_quote or ms.source_clause_quote
    return ms.source_clause_quote


# --- line items -----------------------------------------------------------------------------


def _line_items(
    f: _Facts, contract: ContractTerms, ms: ExtractedMilestoneSpec
) -> list[InvoiceLineItemSpec]:
    items: list[InvoiceLineItemSpec] = []
    if f.math.shortfall_amount > 0:
        items.append(
            InvoiceLineItemSpec(
                name=_clip(f"Milestone shortfall - {f.code}", ITEM_NAME_MAX_CHARS),
                description=_clip(
                    f"Outstanding balance on {f.code} {f.title} ({f.expected} expected less "
                    f"{f.captured} received), per {section_ref(ms.source_clause_quote)}.",
                    ITEM_DESCRIPTION_MAX_CHARS,
                ),
                unit_amount_value=f.math.shortfall_amount,
                currency_code=f.currency,
            )
        )
    if f.math.penalty_amount > 0:
        if f.days_after_due > f.grace_days:
            grace = (
                f"after the {f.grace_days}-day grace period"
                if f.grace_days
                else "after the due date"
            )
            reason = f"payment received {_days(f.days_after_due)} late, {grace}"
        else:
            reason = "partial payment is not permitted"
        items.append(
            InvoiceLineItemSpec(
                name=_clip(f"Late fee ({f.fee_pct}) - {f.code}", ITEM_NAME_MAX_CHARS),
                description=_clip(
                    f"{f.fee_pct} late fee on the full {f.expected} milestone balance ({reason}), "
                    f"per {section_ref(contract.penalty_clause_quote)}.",
                    ITEM_DESCRIPTION_MAX_CHARS,
                ),
                unit_amount_value=f.math.penalty_amount,
                currency_code=f.currency,
            )
        )
    return items


# --- deterministic template -------------------------------------------------------------------


def _late_fee_sentence(f: _Facts) -> str:
    if f.math.penalty_amount > 0:
        return f"{f.section} applies a {f.fee_pct} late fee of {f.penalty}."
    return "The contract states no late fee."


def _due_sentence(f: _Facts) -> str:
    return (
        f"Total due: {f.total}."
        if f.math.total_remediation_amount > 0
        else "Nothing further is due."
    )


def template_summary(f: _Facts) -> str:
    kind = f.math.discrepancy_type
    grace = (
        f"beyond the {f.grace_days}-day grace period"
        if f.grace_days
        else "no grace period applies"
    )
    if kind is DiscrepancyType.UNDERPAID_AND_LATE:
        text = (
            f"Client paid {f.captured} against the {f.expected} {f.code} milestone ({f.shortfall} shortfall) "
            f"on {f.captured_on}, {_days(f.days_after_due)} after the {f.due} due date, {grace}. "
            f"{_late_fee_sentence(f)} {_due_sentence(f)}"
        )
    elif kind is DiscrepancyType.LATE_PAYMENT:
        text = (
            f"The {f.expected} {f.code} milestone was paid in full on {f.captured_on}, "
            f"{_days(f.days_after_due)} after the {f.due} due date; {grace}. "
            f"{_late_fee_sentence(f)} {_due_sentence(f)}"
        )
    elif kind is DiscrepancyType.UNDERPAID:
        partial = (
            f" Partial payment is not permitted, so {f.section} applies a {f.fee_pct} late fee of {f.penalty}."
            if f.math.penalty_amount > 0
            else ""
        )
        text = (
            f"{f.code} was paid {f.captured} against the {f.expected} expected ({f.shortfall} shortfall) "
            f"on {f.captured_on}, within the payment window.{partial} {_due_sentence(f)}"
        )
    elif kind is DiscrepancyType.CURRENCY_MISMATCH:
        text = (
            f"{f.code} was settled as {f.captured} instead of the contractual {f.currency}. "
            f"Under {f.section} the milestone is not compliant until the full {f.expected} is received "
            f"in {f.currency}; no supplemental amount is invoiced automatically."
        )
    else:
        timing = (
            f"on or before the {f.due} due date"
            if f.days_after_due <= 0
            else f"within the {f.grace_days}-day grace period after the {f.due} due date"
        )
        text = f"{f.code} was paid in full ({f.captured}) on {f.captured_on}, {timing}. No action is needed."
    return _clip(text, SUMMARY_MAX_CHARS)


def template_note_body(f: _Facts, summary: str) -> str:
    agreement = f.contract_title.rstrip(". ")
    client = f.client_name.rstrip(". ")
    if client and client.casefold() not in agreement.casefold():
        agreement = f"{agreement} with {client}"
    return f"This supplemental invoice relates to milestone {f.code} ({f.title}) under the {agreement}. {summary}"


def note_footer(f: _Facts) -> str:
    lines: list[str] = []
    if f.clause:
        lines.append(
            f'{FOOTER_CLAUSE_PREFIX}"{_clip(f.clause, _CLAUSE_IN_FOOTER_MAX_CHARS)}"'
        )
    m = f.math
    if m.total_remediation_amount > 0:
        parts = []
        if m.shortfall_amount > 0:
            parts.append(f"shortfall {f.shortfall}")
        if m.penalty_amount > 0:
            parts.append(f"late fee {f.penalty}")
        lines.append(f"{FOOTER_AMOUNT_PREFIX}{f.total} ({' + '.join(parts)})")
    elif m.discrepancy_type is DiscrepancyType.CURRENCY_MISMATCH:
        lines.append(
            f"{FOOTER_AMOUNT_PREFIX}none on this invoice; "
            f"settle the full {f.expected} in {f.currency}."
        )
    return "\n".join(lines)


# --- validation of model text -----------------------------------------------------------------

_CURRENCY_AMOUNT = re.compile(
    r"(?:\$|\b[A-Z]{3} ?)(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\d%])"
)
_BARE_DECIMAL = re.compile(
    r"(?<![\d.,$])(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})(?!\d|\s?%)"
)


def _stated_amounts(text: str) -> set[Decimal]:
    found = _CURRENCY_AMOUNT.findall(text) + _BARE_DECIMAL.findall(text)
    return {Decimal(value.replace(",", "")) for value in found}


def check_prose(prose: AuditProse, f: _Facts, note_budget: int) -> None:
    summary = prose.ai_audit_summary.strip()
    note = prose.paypal_invoice_detail_note.strip()
    if not summary or not note:
        raise ProseRejected("empty text")
    if len(summary) > SUMMARY_MAX_CHARS:
        raise ProseRejected(f"summary is {len(summary)} chars")
    if len(note) > note_budget:
        raise ProseRejected(f"note is {len(note)} chars (budget {note_budget})")

    lowered = (summary + "\n" + note).lower()
    leaked = [key for key in f.as_prompt_facts([]) if key in lowered]
    if leaked:
        raise ProseRejected(f"mentions internal field names: {leaked}")

    m = f.math
    allowed = {
        m.expected_amount,
        m.captured_amount,
        m.shortfall_amount,
        m.penalty_amount,
        m.total_remediation_amount,
    }
    invented = _stated_amounts(summary + "\n" + note) - allowed
    if invented:
        raise ProseRejected(
            f"states amounts not computed by the engine: {sorted(invented)}"
        )

    if (
        m.total_remediation_amount > 0
        and f"{m.total_remediation_amount:,.2f}"
        not in summary.replace(
            f"{m.total_remediation_amount:.2f}", f"{m.total_remediation_amount:,.2f}"
        )
    ):
        raise ProseRejected("summary omits the total due")
    if (
        m.discrepancy_type is DiscrepancyType.CURRENCY_MISMATCH
        and f.captured_currency not in summary
    ):
        raise ProseRejected("summary omits the captured currency")


# --- public entry point -------------------------------------------------------------------------


async def evaluate_payment_compliance(
    contract: ContractTerms,
    milestone: ExtractedMilestoneSpec,
    captured_amount: Decimal,
    captured_currency: str,
    captured_date: date,
    payer_note: str = "",
    *,
    writers: Sequence[AuditProseWriter] | None = None,
    budget_seconds: float = AUDIT_BUDGET_SECONDS,
    provider_timeout: float = AUDIT_PROVIDER_TIMEOUT_SECONDS,
    retry_delay: float = TRANSIENT_RETRY_DELAY_SECONDS,
) -> DiscrepancyEvaluationResult:
    """Audit one authoritative PayPal capture against one milestone.

    ``captured_*`` must come from a COMPLETED capture or a verified webhook, never the browser.
    A ``datetime`` capture time is accepted and reduced to its date (callers pass UTC).
    """
    if isinstance(captured_date, datetime):
        captured_date = captured_date.date()
    paid_in = captured_currency.strip().upper()

    math = calculate_discrepancy_math(
        expected_amount=milestone.expected_amount,
        captured_amount=captured_amount,
        due_date=milestone.due_date,
        captured_date=captured_date,
        grace_period_days=contract.grace_period_days,
        late_fee_percentage=contract.late_fee_percentage,
        allow_partial_payments=contract.allow_partial_payments,
        contract_currency=contract.currency,
        captured_currency=paid_in,
    )
    cur = contract.currency
    facts = _Facts(
        math=math,
        currency=cur,
        captured_currency=paid_in,
        code=milestone.milestone_code,
        title=milestone.title,
        contract_title=contract.contract_title,
        client_name=contract.client_name,
        expected=format_money(math.expected_amount, cur),
        captured=format_money(math.captured_amount, paid_in),
        shortfall=format_money(math.shortfall_amount, cur),
        penalty=format_money(math.penalty_amount, cur),
        total=format_money(math.total_remediation_amount, cur),
        fee_pct=f"{contract.late_fee_percentage:.2f}%",
        due=milestone.due_date.isoformat(),
        captured_on=captured_date.isoformat(),
        days_after_due=(captured_date - milestone.due_date).days,
        grace_days=contract.grace_period_days,
        partial_allowed=contract.allow_partial_payments,
        clause=_violated_clause(math, contract, milestone),
    )
    items = _line_items(facts, contract, milestone)
    summary = template_summary(facts)

    if math.is_compliant:
        return _result(math, facts, summary=summary, note="", items=[])

    footer = note_footer(facts)
    note_budget = INVOICE_NOTE_MAX_CHARS - (len(footer) + 2 if footer else 0)
    note_body = _clip(template_note_body(facts, summary), note_budget)

    chain = default_writers() if writers is None else list(writers)
    if chain:
        prompt = build_audit_prompt(
            facts.as_prompt_facts(items), facts.clause, payer_note
        )

        async def attempt(writer: AuditProseWriter, timeout: float) -> AuditProse:
            prose = await writer.write_audit(AUDIT_SYSTEM_PROMPT, prompt, timeout)
            check_prose(prose, facts, note_budget)
            return prose

        written = await run_with_failover(
            chain,
            attempt,
            purpose="audit prose",
            budget_seconds=budget_seconds,
            provider_timeout=provider_timeout,
            min_slot=min(MIN_PROVIDER_SLOT_SECONDS, provider_timeout),
            retry_delay=retry_delay,
        )
        if written is not None:
            summary = written.ai_audit_summary.strip()
            note_body = written.paypal_invoice_detail_note.strip()

    note = f"{note_body}\n\n{footer}" if footer else note_body
    return _result(
        math, facts, summary=summary, note=note[:INVOICE_NOTE_MAX_CHARS], items=items
    )


def _result(
    math: DiscrepancyMath,
    f: _Facts,
    *,
    summary: str,
    note: str,
    items: list[InvoiceLineItemSpec],
) -> DiscrepancyEvaluationResult:
    return DiscrepancyEvaluationResult(
        discrepancy_type=math.discrepancy_type,
        discrepancy_status=math.discrepancy_status,
        is_compliant=math.is_compliant,
        expected_amount=math.expected_amount,
        captured_amount=math.captured_amount,
        captured_currency=math.captured_currency,
        shortfall_amount=math.shortfall_amount,
        penalty_amount=math.penalty_amount,
        total_remediation_amount=math.total_remediation_amount,
        violated_clause_quote=f.clause,
        ai_audit_summary=summary,
        paypal_invoice_detail_note=note,
        remediation_line_items=items,
    )

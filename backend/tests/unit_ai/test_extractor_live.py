"""Opt-in TST-03 against the real Gemini models (costs a few free-tier calls).

Runs only with RUN_LIVE_LLM=1 and GEMINI_API_KEY in the environment or the repo-root .env:

    RUN_LIVE_LLM=1 python -m pytest backend/tests/unit_ai/test_extractor_live.py -v

The single-model tests accept a transient 503/429 from Google as a skip, not a failure:
that is the exact case the retry + backup chain exists for, and it is covered by
``test_default_chain_extracts_fixture_terms``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from backend.app.ai.extractor import (
    DEFAULT_GEMINI_FALLBACK_MODEL,
    DEFAULT_GEMINI_MODEL,
    LLM_CONFIDENCE,
    GeminiProvider,
    extract_contract_rules,
)
from backend.app.schemas.contracts import ContractExtractionResult

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "shared" / "fixtures" / "contracts"
FIXTURE_JSON = ROOT / "shared" / "fixtures" / "json"
NAMES = {
    "Acme_Standard": "acme_extract.json",
    "Globex_LateFee": "globex_extract.json",
    "Initech_MultiMilestone": "initech_extract.json",
}


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


_load_env()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if GEMINI_KEY.lower().startswith("your_"):
    GEMINI_KEY = ""

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_LLM") != "1",
        reason="set RUN_LIVE_LLM=1 to call real models",
    ),
    pytest.mark.skipif(not GEMINI_KEY, reason="GEMINI_API_KEY not configured"),
]


def _assert_matches_fixture(result: ContractExtractionResult, name: str) -> None:
    fx: dict[str, Any] = json.loads(
        (FIXTURE_JSON / NAMES[name]).read_text(encoding="utf-8")
    )["extraction"]
    assert result.client_paypal_email == fx["client_paypal_email"]
    assert result.late_fee_percentage == Decimal(fx["late_fee_percentage"])
    assert result.grace_period_days == fx["grace_period_days"]
    assert result.allow_partial_payments is fx["allow_partial_payments"]
    assert [
        (m.milestone_code, m.expected_amount, m.due_date) for m in result.milestones
    ] == [
        (
            m["milestone_code"],
            Decimal(m["expected_amount"]),
            date.fromisoformat(m["due_date"]),
        )
        for m in fx["milestones"]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [DEFAULT_GEMINI_MODEL, DEFAULT_GEMINI_FALLBACK_MODEL])
@pytest.mark.parametrize("name", sorted(NAMES))
async def test_single_model_extracts_fixture_terms(
    model: str, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    result = await extract_contract_rules(
        (CONTRACTS / f"{name}.pdf").read_bytes(),
        f"{name}.pdf",
        providers=[GeminiProvider(GEMINI_KEY, model)],
    )
    if result.extraction_confidence != LLM_CONFIDENCE:
        log = caplog.text
        if any(code in log for code in ("503", "429", "TimeoutError")):
            pytest.skip(
                f"{model} unavailable right now (transient): regex fallback served the request"
            )
        pytest.fail(f"{model} answer was rejected by verification:\n{log}")
    _assert_matches_fixture(result, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(NAMES))
async def test_default_chain_extracts_fixture_terms(name: str) -> None:
    """The production path: primary + retry, backup model + retry, then regex. Never fails."""
    start = time.perf_counter()
    result = await extract_contract_rules(
        (CONTRACTS / f"{name}.pdf").read_bytes(), f"{name}.pdf"
    )
    elapsed = time.perf_counter() - start
    assert elapsed < 8.5, f"chain took {elapsed:.1f}s"
    _assert_matches_fixture(result, name)

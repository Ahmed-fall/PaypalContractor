"""Opt-in TST-06 / REQ-AI-02 against real Gemini (a few free-tier calls).

    RUN_LIVE_LLM=1 python -m pytest backend/tests/unit_ai/test_auditor_live.py -v -s

Whatever the model writes (or if Google is overloaded and the template is used), the
amounts, outcome and clause citation must be exactly the engine's.
"""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import pytest

from backend.app.ai.auditor_agent import evaluate_payment_compliance

from ._auditor_helpers import ACME, GLOBEX, HOSTILE_NOTE, INITECH, D, milestone

ROOT = Path(__file__).resolve().parents[3]

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_LLM") != "1",
        reason="set RUN_LIVE_LLM=1 to call real models",
    ),
    pytest.mark.skipif(
        not _KEY or _KEY.lower().startswith("your_"),
        reason="GEMINI_API_KEY not configured",
    ),
]

CASES = {
    "acme-hostile-note": (
        ACME,
        "MS-01",
        "850.00",
        date(2026, 10, 20),
        HOSTILE_NOTE,
        ("150.00", "50.00", "200.00"),
    ),
    "globex-late": (
        GLOBEX,
        "MS-01",
        "2500.00",
        date(2026, 11, 4),
        "",
        ("0.00", "175.00", "175.00"),
    ),
    "initech-partial": (
        INITECH,
        "MS-02",
        "1500.00",
        date(2026, 11, 9),
        "",
        ("300.00", "0.00", "300.00"),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_real_model_cannot_change_locked_outcome(case: str) -> None:
    contract, code, paid, when, note, want = CASES[case]
    start = time.perf_counter()
    r = await evaluate_payment_compliance(
        contract, milestone(contract, code), D(paid), "USD", when, payer_note=note
    )
    elapsed = time.perf_counter() - start

    got: tuple[D, D, D] = (
        r.shortfall_amount,
        r.penalty_amount,
        r.total_remediation_amount,
    )
    assert got == tuple(D(x) for x in want)
    assert r.is_compliant is False
    assert r.violated_clause_quote in r.paypal_invoice_detail_note
    assert f"{r.total_remediation_amount:,.2f}" in r.ai_audit_summary
    assert (
        "ignore previous"
        not in (r.ai_audit_summary + r.paypal_invoice_detail_note).lower()
    )
    assert elapsed < 7
    print(
        f"\n[{case}] {elapsed:.1f}s\nSUMMARY: {r.ai_audit_summary}\nNOTE: {r.paypal_invoice_detail_note}"
    )

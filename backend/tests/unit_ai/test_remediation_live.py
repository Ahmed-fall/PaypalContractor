"""Opt-in checks of remediation tool calling against real Gemini (a few requests).

    RUN_LIVE_LLM=1 python -m pytest backend/tests/unit_ai/test_remediation_live.py -v -s

Google availability varies (free tier: 20 requests/day/model, frequent 503s), so a
transient error skips; a schema rejection (HTTP 400) fails.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from backend.app.ai.auditor_agent import evaluate_payment_compliance
from backend.app.ai.llm import DEFAULT_GEMINI_MODEL, GeminiModel, configured_key
from backend.app.ai.paypal_tools import (
    REMEDIATION_TOOL_NAMES,
    build_create_invoice_params,
    tool_declarations,
)
from backend.app.ai.remediation_agent import run_remediation

from ._auditor_helpers import ACME, ACME_CAPTURE, milestone
from .test_remediation_run import FakeExecutor

ROOT = Path(__file__).resolve().parents[3]

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

KEY = configured_key(os.environ.get("GEMINI_API_KEY")) or ""
pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_LLM") != "1",
        reason="set RUN_LIVE_LLM=1 to call real models",
    ),
    pytest.mark.skipif(not KEY, reason="GEMINI_API_KEY not configured"),
]
TRANSIENT = ("429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE")


@pytest.mark.asyncio
async def test_gemini_accepts_tool_declarations() -> None:
    model = GeminiModel(KEY, os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL)
    try:
        calls = await asyncio.wait_for(
            model.generate_tool_calls(
                "You plan PayPal invoice remediation steps using the tools provided.",
                "Remediate discrepancy dsc_TEST: create the invoice, send it, then "
                "generate its QR code. Use invoice_id INV2-TEST for steps 2 and 3.",
                tool_declarations()[:3],
                REMEDIATION_TOOL_NAMES,
            ),
            timeout=20,
        )
    except TimeoutError:
        pytest.skip("Gemini timed out (transient)")
    except Exception as exc:
        if any(code in str(exc) for code in TRANSIENT):
            pytest.skip(f"Gemini unavailable right now: {str(exc)[:120]}")
        raise
    print(f"\nGemini proposed: {[c.name for c in calls]}")
    assert calls, "Gemini returned no tool calls"
    assert {c.name for c in calls} <= set(REMEDIATION_TOOL_NAMES)


@pytest.mark.asyncio
async def test_live_remediation_executes_canonical_args() -> None:
    """Full run with the default Gemini planners and a fake PayPal executor."""
    evaluation = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[]
    )
    executor = FakeExecutor()
    start = time.perf_counter()
    result = await run_remediation(
        ACME,
        milestone(ACME),
        evaluation,
        executor,
        discrepancy_id="dsc_LIVE_CHECK",
        confirm_send=True,
    )
    elapsed = time.perf_counter() - start
    print(f"\nplanned_by={result.planned_by} in {elapsed:.1f}s")
    assert executor.calls == list(REMEDIATION_TOOL_NAMES)
    assert executor.create_params == build_create_invoice_params(
        ACME, milestone(ACME), evaluation
    )
    assert elapsed < 8  # 7 s planning budget + fake PayPal calls

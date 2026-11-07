"""H2 + Payouts dispatch resolve the real Leash modules when run as ``backend.app``.

They used to import ``app.leash...`` absolutely, which only resolves with
``backend/`` as the cwd; under ``backend.app`` the hooks silently became no-ops
and the Leash router was never mounted.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from backend.app import leash_hooks
from backend.app.leash.payroll import runs
from backend.app.services.paypal import payouts_sync


def test_import_optional_resolves_against_the_package() -> None:
    assert leash_hooks.import_optional(".leash.payroll.runs") is runs
    assert leash_hooks.import_optional(".services.paypal.payouts_sync") is payouts_sync


def test_missing_target_module_is_a_no_op() -> None:
    assert leash_hooks.import_optional(".leash.payroll.not_landed_yet") is None
    assert leash_hooks.import_optional(".not_a_package.module") is None


def test_other_import_errors_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(name: str, package: str | None = None) -> Any:
        raise ModuleNotFoundError("No module named 'some_dependency'", name="some_dependency")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError, match="some_dependency"):
        leash_hooks.import_optional(".leash.payroll.runs")


def test_h2_calls_the_real_runs_module(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def trigger(milestone_id: str) -> str:
        seen.append(milestone_id)
        return "prn_TEST"

    monkeypatch.setattr(runs, "on_milestone_payment", trigger)
    assert asyncio.run(leash_hooks.on_milestone_payment("ms_seed_01")) == "prn_TEST"
    assert seen == ["ms_seed_01"]


def test_payouts_webhook_reaches_payouts_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def handler(event: dict[str, Any]) -> None:
        seen.append(event)

    monkeypatch.setattr(payouts_sync, "handle_webhook_event", handler)
    event = {"id": "WH-1", "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED"}
    assert asyncio.run(leash_hooks.dispatch_payouts_event(event)) is True
    assert seen == [event]


def test_leash_router_is_mounted() -> None:
    from backend.app.main import app

    assert "/api/v1/leash/policy" in app.openapi()["paths"]

"""Adapters to the Leash package (P5): H1 ``decide()``, the H2 payroll trigger and
Payouts webhook dispatch.

Every hook is import-guarded: a missing or broken Leash module degrades to a
logged no-op and never breaks the Detective path. ``LEASH_ENABLED=false`` turns
the hooks off entirely.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, cast

from .config import get_settings

logger = logging.getLogger(__name__)

PAYOUTS_EVENT_PREFIX = "PAYMENT.PAYOUTS"


def leash_enabled() -> bool:
    return get_settings().leash_enabled


async def decide(action: Any, facts: dict[str, Any], subject_id: str) -> Any:
    """Hook H1: P5's ``leash.decide`` (pure evaluate + explanation + log row)."""
    if not leash_enabled():
        return None
    from . import leash

    return await leash.decide(action, facts, subject_id)


async def on_milestone_payment(milestone_id: str) -> str | None:
    """Hook H2: P5's ``leash.payroll.runs.on_milestone_payment`` when it lands."""
    if not leash_enabled():
        return None
    try:
        module = importlib.import_module("app.leash.payroll.runs")
    except ImportError:
        logger.info(
            "leash.payroll_pending",
            extra={"hint": "P5's leash/payroll/runs.py has not landed yet"},
        )
        return None
    trigger = getattr(module, "on_milestone_payment", None)
    if not callable(trigger):
        logger.info(
            "leash.payroll_pending",
            extra={"hint": "runs.on_milestone_payment not implemented yet"},
        )
        return None
    payload = await trigger(milestone_id)
    return cast("str | None", payload)


async def dispatch_payouts_event(event: dict[str, Any]) -> bool:
    """Route a ``PAYMENT.PAYOUTS*`` webhook payload to P5's sync module."""
    if not leash_enabled():
        return False
    try:
        module = importlib.import_module("app.services.paypal.payouts_sync")
    except ImportError:
        logger.info(
            "leash.payouts_sync_pending",
            extra={"hint": "P5's payouts_sync.py has not landed yet"},
        )
        return False
    handler = getattr(module, "handle_webhook_event", None)
    if not callable(handler):
        logger.info(
            "leash.payouts_sync_pending",
            extra={"hint": "no handle_webhook_event export"},
        )
        return False
    await handler(event)
    return True

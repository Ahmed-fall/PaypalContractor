"""Health + demo endpoints (WP-1.4): GET /health, POST /demo/reset.

Health caches the sandbox token ping for 60 s; when PayPal is unreachable the
endpoint answers 503 with ``PAYPAL_UNAVAILABLE`` and ``status: degraded``. } .
``POST /demo/reset`` wipes every table and re-seeds the Acme contract —
judge-facing, demo-key only (A.6). The Leash demo state goes first (payroll runs
reference milestones) and is re-seeded with it; Leash decisions are kept.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import inspect

from ..api.deps import require_demo_key
from ..api.errors import ApiError
from ..db.models import Contract, Discrepancy, Milestone, PaypalCapture, RemediationInvoice, WebhookEvent
from ..db.seed import seed_demo
from ..leash_hooks import import_optional, leash_enabled
from ..schemas.contracts import HealthResponse

logger = logging.getLogger(__name__)

_HEALTH_TTL_SECONDS = 60
_health_cache: dict[str, float | bool] = {"checked_at": 0.0, "connected": False}

router = APIRouter(tags=["health", "demo"])


@router.get("/health", status_code=200)
async def health() -> HealthResponse:
    now = time.monotonic()
    if now - float(_health_cache["checked_at"]) > _HEALTH_TTL_SECONDS:
        connected = False
        try:
            connected = await _ping_paypal()
        except Exception as exc:
            logger.warning("health.paypal_unreachable", extra={"error": str(exc)[:160]})
        _health_cache["checked_at"] = now
        _health_cache["connected"] = connected

    connected = bool(_health_cache["connected"])
    payload = HealthResponse(
        status="ok" if connected else "degraded",
        paypal_sandbox_connected=connected,
        leash_enabled=leash_enabled(),
        ai_provider="gemini" if os.environ.get("GEMINI_API_KEY") else None,
    )
    if not connected:
        raise ApiError(503, "PAYPAL_UNAVAILABLE", "the PayPal sandbox is unreachable")
    return payload


async def _ping_paypal() -> bool:
    from ..services.paypal import get_auth_manager

    return bool(await get_auth_manager().get_access_token())


@router.post("/demo/reset", status_code=200, dependencies=[Depends(require_demo_key)])
def demo_reset() -> dict[str, object]:
    wiped: dict[str, int] = _reset_leash()
    # Children before parents: invoices -> discrepancies -> captures -> milestones.
    order = (RemediationInvoice, Discrepancy, PaypalCapture, Milestone, Contract, WebhookEvent)
    for model in order:
        wiped[model.__tablename__] = _wipe(model)
    seed_demo(force=True)
    logger.info("demo.reset", extra={"wiped": wiped})
    return {"status": "reset", "wiped": wiped}


def _reset_leash() -> dict[str, int]:
    """P5's demo tables, before the Detective wipe (``payroll_runs`` -> ``milestones``)."""
    from ..db.database import SessionLocal, engine

    seed = import_optional(".leash.seed")
    if seed is None or not inspect(engine).has_table("payroll_runs"):
        return {}
    with SessionLocal() as db:
        return dict(seed.reset_leash_demo(db))


def _wipe(model: type[Any]) -> int:
    from ..db.database import SessionLocal

    with SessionLocal() as db:
        rows = db.query(model).delete()
        db.commit()
        return int(rows or 0)

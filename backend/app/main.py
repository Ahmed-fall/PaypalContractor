"""The Financial Detective — FastAPI application factory (WP-1.5).

Lifespan owns the database bootstrap (``create_all`` + demo seed, A.9 note) and
the shared PayPal HTTP client. P5's ``api/leash.py`` is mounted only when the
module exists and ``LEASH_ENABLED`` is on — the Detective keeps working when the
Leash package is not on the trunk yet.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.errors import RequestIdMiddleware, install_error_handlers
from .config import get_settings
from .db.database import init_db
from .leash_hooks import leash_enabled
from .services.paypal import close_http_client as _close_paypal_http

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"  # A.2: every route lives under the versioned base


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    logger.info("backend.ready")
    try:
        yield
    finally:
        await _close_paypal_http()
        logger.info("backend.stopped")


def _mount_leash(app: FastAPI) -> None:
    """P5's router mounts beside the Detective routes (WP-5.4 hook H2 owner)."""
    if not leash_enabled():
        logger.info("leash.mount_skipped", extra={"reason": "LEASH_ENABLED=false"})
        return
    try:
        module = importlib.import_module("app.api.leash")
    except ModuleNotFoundError:
        logger.info("leash.mount_skipped", extra={"reason": "module not present"})
        return
    router = getattr(module, "router", None)
    if router is None:
        logger.info("leash.mount_skipped", extra={"reason": "no router"})
        return
    app.include_router(router)
    logger.info("leash.mounted")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="The Financial Detective",
        description="Contract-to-payment compliance auditing + remediation for B2B contracts.",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)

    from .api.contracts import router as contracts_router
    from .api.discrepancies import router as discrepancies_router
    from .api.health import router as health_router
    from .api.ledger import router as ledger_router
    from .api.simulator import router as simulator_router
    from .api.webhooks import router as webhooks_router

    for router in (
        contracts_router,
        simulator_router,
        webhooks_router,
        ledger_router,
        discrepancies_router,
        health_router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    _mount_leash(app)  # api/leash.py already carries /api/v1/leash

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": "the-financial-detective",
            "docs": "/docs",
            "health": f"{API_PREFIX}/health",
        }

    return app


app = create_app()

"""One error body for every non-2xx response (A.4) + the request-id middleware."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import FastAPI, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..services.paypal._client import PayPalAPIError, PayPalUnavailable
from ..services.paypal.auth import PayPalAuthError
from ..services.paypal.webhooks import WebhookVerificationError

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


class ApiError(Exception):
    """Stable-code error the frontend can switch on (A.4)."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_body(code: str, message: str, details: Any, request_id: str) -> dict[str, Any]:
    encoded = jsonable_encoder(
        {"error": {"code": code, "message": message, "details": details, "request_id": request_id}}
    )
    return cast("dict[str, Any]", encoded)


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or f"req_{uuid.uuid4().hex[:12]}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details, request.state.request_id),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(part) for part in err.get("loc", [])), "msg": err.get("msg")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_body(
                "VALIDATION_ERROR", "request body or query failed validation", {"fields": fields}, request.state.request_id
            ),
        )

    @app.exception_handler(WebhookVerificationError)
    async def _webhook_signature(request: Request, exc: WebhookVerificationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=error_body("WEBHOOK_SIGNATURE_INVALID", str(exc) or "PayPal verification failed", None, request.state.request_id),
        )

    @app.exception_handler(PayPalAPIError)
    async def _paypal_api_error(request: Request, exc: PayPalAPIError) -> JSONResponse:
        logger.warning("paypal.error", extra={"paypal_status": exc.status_code, "paypal_debug_id": exc.debug_id})
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=error_body(
                "PAYPAL_ERROR",
                exc.message,
                {"paypal_name": exc.name, "paypal_debug_id": exc.debug_id},
                request.state.request_id,
            ),
        )

    @app.exception_handler(PayPalUnavailable)
    @app.exception_handler(PayPalAuthError)
    async def _paypal_unavailable(request: Request, exc: Exception) -> JSONResponse:
        logger.warning("paypal.unavailable", extra={"exc": exc})
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=error_body("PAYPAL_UNAVAILABLE", "PayPal is unreachable; retry shortly.", None, request.state.request_id),
        )

    @app.exception_handler(Exception)
    async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("internal.error", extra={"request_id": getattr(request.state, "request_id", None)})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("INTERNAL_ERROR", "unexpected server error", None, request.state.request_id),
        )

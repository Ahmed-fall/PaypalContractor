"""Shared async PayPal REST client for every service module (A.4 retry rules).

- 15 s timeout on every call; ``paypal_debug_id`` is logged from the headers.
- 401: refresh the token once and retry once, else ``PayPalAPIError``.
- 5xx or timeout: retry twice (1 s, then 3 s) with the SAME ``PayPal-Request-Id``
  so PayPal returns the original result instead of acting twice.
- 400/422: no retry -> ``PayPalAPIError`` (mapped to 502 ``PAYPAL_ERROR``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ...config import get_settings
from .auth import PayPalAuthError, get_auth_manager

logger = logging.getLogger(__name__)

CALL_TIMEOUT = 15.0
RETRY_DELAY_SECONDS = (1.0, 3.0)

_http_client: httpx.AsyncClient | None = None


class PayPalAPIError(Exception):
    """PayPal answered 4xx that we cannot fix ourselves (map to 502)."""

    def __init__(
        self, status_code: int, name: str, message: str, debug_id: str | None = None
    ) -> None:
        super().__init__(f"PayPal {name} ({status_code}): {message}")
        self.status_code = status_code
        self.name = name
        self.message = message
        self.debug_id = debug_id


class PayPalUnavailable(Exception):
    """PayPal 5xx or timeout after every retry (map to 503)."""


def get_http_client() -> httpx.AsyncClient:
    """Process-wide HTTP client; closed by the FastAPI lifespan on shutdown."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=CALL_TIMEOUT)
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def _first_issue(body: Any) -> str:
    if isinstance(body, dict):
        details = body.get("details")
        if isinstance(details, list) and details and isinstance(details[0], dict):
            issue = str(details[0].get("issue", "")).strip()
            if issue:
                return str(body.get("name", "ERROR")) + ": " + issue
        if body.get("message"):
            return str(body["message"])
    return "unexpected PayPal response"


async def paypal_request(
    method: str,
    path: str,
    *,
    request_id: str,
    json_body: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One authenticated PayPal JSON call with the A.4 retry ladder."""
    settings = get_settings()
    url = f"{settings.paypal_api_base_url}{path}"
    client = get_http_client()

    for attempt in range(3):
        token = await get_auth_manager().get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": request_id,
        }
        try:
            response = await client.request(
                method, url, json=json_body, headers=headers, timeout=CALL_TIMEOUT
            )
        except httpx.TimeoutException as exc:
            logger.warning(
                "paypal.timeout",
                extra={
                    "paypal_op": path,
                    "paypal_request_id": request_id,
                    "attempt": attempt,
                },
            )
            if attempt < 2:
                await asyncio.sleep(RETRY_DELAY_SECONDS[attempt])
                continue
            raise PayPalUnavailable(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise PayPalUnavailable(str(exc)) from exc

        debug_id = response.headers.get("paypal-debug-id")
        logger.info(
            "paypal.call",
            extra={
                "paypal_op": path,
                "paypal_request_id": request_id,
                "paypal_status": response.status_code,
                "paypal_debug_id": debug_id,
            },
        )

        if response.status_code in (httpx.codes.OK, httpx.codes.CREATED):
            return response.json() if response.content else {}

        if response.status_code == httpx.codes.UNAUTHORIZED and attempt == 0:
            get_auth_manager().invalidate()  # stale token: refresh, retry same request
            continue

        if response.status_code >= 500 and attempt < 2:
            await asyncio.sleep(RETRY_DELAY_SECONDS[attempt])
            continue

        body: Any = response.json() if response.content else None
        raise PayPalAPIError(
            response.status_code,
            str(body.get("name", "ERROR")) if isinstance(body, dict) else "ERROR",
            _first_issue(body),
            debug_id,
        )

    raise PayPalAuthError(401, "token refresh did not resolve the 401")

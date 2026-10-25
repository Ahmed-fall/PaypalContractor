"""OAuth2 client-credentials token cache shared by every PayPal call (REQ-PP-01).

No other code fetches its own token: Orders, Webhooks, Invoicing and Payouts all call
``get_auth_manager().get_access_token()``. The token is refreshed when fewer than
300 s remain, or after a caller reports a 401 through ``invalidate()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from functools import lru_cache

import httpx

from ...config import Settings, get_settings

logger = logging.getLogger(__name__)

REFRESH_MARGIN_SECONDS = 300
TOKEN_TIMEOUT_SECONDS = 15.0


class PayPalAuthError(Exception):
    """PayPal refused or could not issue an access token."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"PayPal OAuth failed ({status_code}): {message}")
        self.status_code = status_code


class PayPalAuthManager:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._client = client
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        token = self._fresh_token()
        if token is not None:
            return token
        async with self._lock:
            # Another task may have refreshed while this one waited for the lock.
            token = self._fresh_token()
            if token is None:
                token = await self._fetch_token()
        return token

    def invalidate(self) -> None:
        """Drop the cached token; call after PayPal answers 401 to a request."""
        self._token = None
        self._expires_at = 0.0

    def _fresh_token(self) -> str | None:
        if (
            self._token is not None
            and self._expires_at - self._clock() > REFRESH_MARGIN_SECONDS
        ):
            return self._token
        return None

    async def _fetch_token(self) -> str:
        if self._client is not None:
            response = await self._request(self._client)
        else:
            async with httpx.AsyncClient() as client:
                response = await self._request(client)

        if response.status_code != httpx.codes.OK:
            raise PayPalAuthError(response.status_code, _error_message(response))

        payload = response.json()
        token = str(payload["access_token"])
        expires_in = int(payload["expires_in"])
        self._token = token
        self._expires_at = self._clock() + expires_in
        logger.info(
            "paypal.oauth.refreshed",
            extra={
                "paypal_op": "oauth.token",
                "paypal_debug_id": response.headers.get("paypal-debug-id"),
                "expires_in": expires_in,
            },
        )
        return token

    async def _request(self, client: httpx.AsyncClient) -> httpx.Response:
        try:
            return await client.post(
                f"{self._settings.paypal_api_base_url}/v1/oauth2/token",
                auth=(
                    self._settings.paypal_client_id,
                    self._settings.paypal_client_secret.get_secret_value(),
                ),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
                timeout=TOKEN_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise PayPalAuthError(
                503, f"PayPal OAuth unreachable: {type(exc).__name__}"
            ) from exc


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase
    if isinstance(body, dict):
        return str(
            body.get("error_description") or body.get("error") or response.reason_phrase
        )
    return response.reason_phrase


@lru_cache
def get_auth_manager() -> PayPalAuthManager:
    """Process-wide token cache. Use this instead of building a manager."""
    return PayPalAuthManager(get_settings())

"""TST-01 and TST-02 (token half): OAuth2 token caching and refresh, mocked with respx."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from backend.app.config import Settings
from backend.app.services.paypal.auth import PayPalAuthError, PayPalAuthManager

from .conftest import SANDBOX

TOKEN_URL = f"{SANDBOX}/v1/oauth2/token"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def token_response(token: str = "tok-1", expires_in: int = 32_400) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": token, "token_type": "Bearer", "expires_in": expires_in},
        headers={"paypal-debug-id": "dbg123"},
    )


@pytest.mark.asyncio
@respx.mock
async def test_token_is_cached_across_calls(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=token_response())
    manager = PayPalAuthManager(settings)

    assert await manager.get_access_token() == "tok-1"
    assert await manager.get_access_token() == "tok-1"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_request_uses_client_credentials(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=token_response())
    await PayPalAuthManager(settings).get_access_token()

    request = route.calls.last.request
    assert request.headers["authorization"].startswith("Basic ")
    assert request.content == b"grant_type=client_credentials"


@pytest.mark.asyncio
@respx.mock
async def test_tst01_short_ttl_token_is_refreshed(settings: Settings) -> None:
    """TST-01: a cached token with TTL 100 s (< 300 s margin) triggers a fresh fetch."""
    route = respx.post(TOKEN_URL).mock(
        side_effect=[token_response("short", expires_in=100), token_response("fresh")]
    )
    manager = PayPalAuthManager(settings)

    assert await manager.get_access_token() == "short"
    assert await manager.get_access_token() == "fresh"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_refresh_starts_300_seconds_before_expiry(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            token_response("first", expires_in=1_000),
            token_response("second"),
        ]
    )
    clock = FakeClock()
    manager = PayPalAuthManager(settings, clock=clock)

    assert await manager.get_access_token() == "first"
    clock.now += 699  # 301 s left: still fresh
    assert await manager.get_access_token() == "first"
    clock.now += 1  # 300 s left: refresh
    assert await manager.get_access_token() == "second"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_invalidate_after_401_forces_refetch(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[token_response("stale"), token_response("renewed")]
    )
    manager = PayPalAuthManager(settings)

    assert await manager.get_access_token() == "stale"
    manager.invalidate()
    assert await manager.get_access_token() == "renewed"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_callers_share_one_fetch(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=token_response())
    manager = PayPalAuthManager(settings)

    tokens = await asyncio.gather(*(manager.get_access_token() for _ in range(10)))
    assert set(tokens) == {"tok-1"}
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_tst02_bad_secret_raises_401(settings: Settings) -> None:
    """TST-02: a bad secret surfaces as PayPalAuthError(401) and caches nothing."""
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "Client Authentication failed",
            },
        )
    )
    manager = PayPalAuthManager(settings)

    with pytest.raises(PayPalAuthError) as excinfo:
        await manager.get_access_token()
    assert excinfo.value.status_code == 401
    assert "Client Authentication failed" in str(excinfo.value)
    assert "test-client-secret" not in str(excinfo.value)


@pytest.mark.asyncio
@respx.mock
async def test_network_failure_maps_to_503(settings: Settings) -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(PayPalAuthError) as excinfo:
        await PayPalAuthManager(settings).get_access_token()
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
@respx.mock
async def test_failed_refresh_recovers_on_next_call(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(500, text="oops"), token_response("ok")]
    )
    manager = PayPalAuthManager(settings)

    with pytest.raises(PayPalAuthError):
        await manager.get_access_token()
    assert await manager.get_access_token() == "ok"
    assert route.call_count == 2

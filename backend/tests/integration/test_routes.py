"""A.2: every Detective route is served under the ``/api/v1`` base."""

from __future__ import annotations

from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from backend.app.db import Base, engine


@pytest.fixture(autouse=True)
def fresh_tables() -> Any:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client() -> Any:
    from backend.app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_detective_routes_live_under_api_v1() -> None:
    from backend.app.main import app

    # Included routers are resolved lazily, so read the paths from the schema.
    paths = set(app.openapi()["paths"])
    for path in (
        "/api/v1/health",
        "/api/v1/demo/reset",
        "/api/v1/simulator/pay",
        "/api/v1/webhooks/paypal",
        "/api/v1/ledger",
        "/api/v1/discrepancies/{discrepancy_id}/remediate",
    ):
        assert path in paths
    unversioned = {p for p in paths if not p.startswith("/api/v1/")}
    assert unversioned == set()


@respx.mock
def test_health_is_served_under_the_base(client: TestClient) -> None:
    respx.post("https://api-m.sandbox.paypal.com/v1/oauth2/token").mock(
        return_value=Response(200, json={"access_token": "test-token", "expires_in": 10800})
    )
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/health").status_code == 404
    assert client.get("/").json()["health"] == "/api/v1/health"

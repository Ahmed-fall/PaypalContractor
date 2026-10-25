"""Shared fixtures for the PayPal unit tests. No test here reads the real ``.env``."""

from __future__ import annotations

from typing import Any

import pytest

from backend.app.config import Settings

SANDBOX = "https://api-m.sandbox.paypal.com"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "paypal_client_id": "test-client-id",
        "paypal_client_secret": "test-client-secret",
        "demo_key": "d" * 24,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    return make_settings()

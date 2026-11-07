"""TST-02 (startup half): the sandbox-only guardrail and settings parsing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from .conftest import SANDBOX, make_settings


def test_sandbox_url_accepted() -> None:
    assert make_settings().paypal_api_base_url == SANDBOX


def test_trailing_slash_normalised() -> None:
    assert (
        make_settings(paypal_api_base_url=SANDBOX + "/").paypal_api_base_url == SANDBOX
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://api-m.paypal.com",
        "https://api.paypal.com",
        "http://api-m.sandbox.paypal.com",
        "https://api-m.sandbox.paypal.com.evil.example",
        "",
    ],
)
def test_non_sandbox_url_refuses_to_start(url: str) -> None:
    with pytest.raises(ValueError, match="refusing to start"):
        make_settings(paypal_api_base_url=url)


def test_missing_credentials_fail() -> None:
    with pytest.raises(ValidationError):
        make_settings(paypal_client_id="")


def test_short_demo_key_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(demo_key="too-short")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///./fd.db", "sqlite:///./fd.db"),
    ],
)
def test_database_url_uses_psycopg(url: str, expected: str) -> None:
    assert make_settings(database_url=url).database_url == expected


def test_cors_origins_split() -> None:
    settings = make_settings(
        cors_origins="https://fd-web.onrender.com, http://localhost:5173"
    )
    assert settings.cors_origin_list == [
        "https://fd-web.onrender.com",
        "http://localhost:5173",
    ]


def test_secret_not_in_repr() -> None:
    assert "test-client-secret" not in repr(make_settings())


def _luhn_valid(number: str) -> bool:
    digits = [int(char) for char in reversed(number)]
    total = sum(digits[0::2]) + sum(sum(divmod(2 * d, 10)) for d in digits[1::2])
    return total % 10 == 0


def test_luhn_helper_rejects_previous_default() -> None:
    # The old default PayPal answered with 422 "Invalid card number".
    assert not _luhn_valid("4032038441061188")


def test_default_simulator_card_passes_luhn() -> None:
    number = make_settings().simulator_card_number
    assert number.isdigit() and len(number) == 16
    assert _luhn_valid(number)

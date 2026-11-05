"""Orders v2: create + capture for the built-in Client Payment Simulator (WP-1.2).

Every order carries ``custom_id = milestone_id`` and a per-request
``PayPal-Request-Id`` so a retried create or capture never acts twice (PIT-06).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ...config import get_settings
from ._client import paypal_request

CREATE_ORDER_PATH = "/v2/checkout/orders"
CAPTURE_ORDER_PATH = "/v2/checkout/orders/{order_id}/capture"


def capture_fields(response: dict[str, Any]) -> tuple[str, str, Decimal, str, str | None]:
    """(capture_id, order_id, gross, currency, payer_email) from a capture response.

    Raises ``KeyError`` when PayPal answers without a COMPLETED capture body: the
    caller must treat that as authoritative failure (never invent money amounts).
    """
    payments = response["purchase_units"][0]["payments"]["captures"][0]
    payer = response.get("payer") or {}
    return (
        str(payments["id"]),
        str(response["id"]),
        Decimal(str(payments["amount"]["value"])),
        str(payments["amount"]["currency_code"]),
        payer.get("email_address"),
    )


async def create_order(
    *,
    amount: Decimal,
    currency: str,
    milestone_id: str,
    payer_email: str,
    request_id: str,
) -> dict[str, Any]:
    settings = get_settings()
    body: dict[str, Any] = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                "custom_id": milestone_id,
            }
        ],
        "payer": {"email_address": payer_email},
        # Sandbox demo path: a card payment source lets the server capture without
        # a browser approval popup; defaults are PayPal's public sandbox test card.
        "payment_source": {
            "card": {
                "name": "Demo Client",
                "number": settings.simulator_card_number,
                "expiry": settings.simulator_card_expiry,
                "security_code": settings.simulator_card_csc,
            }
        },
    }
    return await paypal_request(
        "POST", CREATE_ORDER_PATH, request_id=request_id, json_body=body
    )


async def capture_order(*, order_id: str, request_id: str) -> dict[str, Any]:
    return await paypal_request(
        "POST", CAPTURE_ORDER_PATH.format(order_id=order_id), request_id=request_id
    )

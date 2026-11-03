"""Webhooks v1: signature verification via PayPal's verify endpoint (WP-1.2).

POST /v1/notifications/verify-webhook-signature carries the five ``PAYPAL-*``
transmission headers, our webhook id and the event object. Only a response whose
``verification_status == "SUCCESS"`` accepts the event; anything else is rejected
with HTTP 400 (REQ-PP-03). The demo-key rule never applies here: the webhook is
authenticated by PayPal's signature alone (A.6).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ._client import paypal_request

VERIFY_PATH = "/v1/notifications/verify-webhook-signature"

TRANSMISSION_HEADERS = (
    "PAYPAL-AUTH-ALGO",
    "PAYPAL-CERT-URL",
    "PAYPAL-TRANSMISSION-ID",
    "PAYPAL-TRANSMISSION-SIG",
    "PAYPAL-TRANSMISSION-TIME",
)

logger = logging.getLogger(__name__)


class WebhookVerificationError(Exception):
    pass


def required_headers_present(headers: Any) -> bool:
    return all(headers.get(name) for name in TRANSMISSION_HEADERS)


def parse_event(raw_body: bytes) -> dict[str, Any]:
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookVerificationError(f"unparsable body: {exc}") from exc
    if not isinstance(event, dict):
        raise WebhookVerificationError("event must be a JSON object")
    return event


async def verify_signature(
    headers: Any, event: dict[str, Any], *, webhook_id: str
) -> None:
    """Raise ``WebhookVerificationError`` unless PayPal itself confirms SUCCESS."""
    if not isinstance(event.get("id"), str) or not event["id"]:
        raise WebhookVerificationError("event has no id")
    payload = {
        "auth_algo": headers["PAYPAL-AUTH-ALGO"],
        "cert_url": headers["PAYPAL-CERT-URL"],
        "transmission_id": headers["PAYPAL-TRANSMISSION-ID"],
        "transmission_sig": headers["PAYPAL-TRANSMISSION-SIG"],
        "transmission_time": headers["PAYPAL-TRANSMISSION-TIME"],
        "webhook_id": webhook_id,
        # Forward the parsed event; the transmission headers identify the exact
        # delivery PayPal signed, including the insertion order of the original bytes.
        "webhook_event": event,
    }
    response = await paypal_request(
        "POST", VERIFY_PATH, request_id="webhook-verification", json_body=payload
    )
    if response.get("verification_status") != "SUCCESS":
        logger.info(
            "webhook.verification_failed",
            extra={"transmission_id": payload["transmission_id"]},
        )
        raise WebhookVerificationError("verification_status != SUCCESS")

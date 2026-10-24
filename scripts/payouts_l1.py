#!/usr/bin/env python3
"""Payouts L1 proof: send a real 2-item PayPal sandbox Payouts batch and verify it.

Run from the repo root: ``python -m scripts.payouts_l1 --to A --to B [--save-fixtures]``.
The token comes only from ``get_auth_manager()``; the base URL from ``get_settings()``
(sandbox-guarded). Full recipient emails are never printed or logged.
"""

from __future__ import annotations

import copy
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "shared" / "fixtures" / "leash" / "paypal"

POLL_INTERVAL_S = 10
POLL_MAX_S = 120
TIMEOUT_S = 15.0

EMAIL_SUBJECT = "Leash L1 sandbox payout test"
NON_TERMINAL_BATCH = {"NEW", "PENDING", "PROCESSING"}
NON_TERMINAL_ITEM = {"PENDING", "ONHOLD"}
_AMOUNT_RE = re.compile(r"[0-9]+\.[0-9]{2}")


def parse_amount(text: str) -> Decimal:
    """Parse a 2-dp positive money string; anything else is rejected."""
    if not _AMOUNT_RE.fullmatch(text):
        raise ValueError(f"amount must look like 1.00, got {text!r}")
    value = Decimal(text)
    if value <= 0:
        raise ValueError("amount must be greater than zero")
    return value


def build_batch(
    run_id: str, recipients: list[str], amount: Decimal, currency: str
) -> dict[str, Any]:
    """Build the A.2.3 Payouts body; item ids are ``prl_l1_<run>_<n>``."""
    value = f"{amount.quantize(Decimal('0.01'))}"
    return {
        "sender_batch_header": {
            "sender_batch_id": f"fd-l1-{run_id}",
            "email_subject": EMAIL_SUBJECT,
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": email.strip().lower(),
                "amount": {"value": value, "currency": currency},
                "note": f"Leash L1 test item {n}",
                "sender_item_id": f"prl_l1_{run_id}_{n}",
            }
            for n, email in enumerate(recipients, start=1)
        ],
    }


def payout_request_id(run_id: str, purpose: str) -> str:
    """Deterministic ``PayPal-Request-Id`` (``payout`` or ``payout-resend``)."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{run_id}:{purpose}"))


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"*@{domain}"
    return f"{local[:2]}***@{domain}"


def redact(payload: Any, recipients: list[str]) -> Any:
    """Deep copy of ``payload`` with each recipient email replaced by a placeholder."""
    patterns = [
        (
            re.compile(re.escape(email.strip()), re.IGNORECASE),
            f"sb-member{i}@personal.example.com",
        )
        for i, email in enumerate(recipients, start=1)
    ]

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            for pattern, placeholder in patterns:
                node = pattern.sub(placeholder, node)
            return node
        if isinstance(node, dict):
            return {walk(k): walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return copy.deepcopy(node)

    return walk(payload)


def is_batch_terminal(batch: dict[str, Any]) -> bool:
    status = (batch.get("batch_header") or {}).get("batch_status")
    if status in NON_TERMINAL_BATCH:
        return False
    return all(
        item.get("transaction_status") not in NON_TERMINAL_ITEM
        for item in batch.get("items") or []
    )

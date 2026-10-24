"""Tests for scripts/payouts_l1.py (Payouts L1 sandbox proof). PayPal is always mocked."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from scripts import payouts_l1

RUN = "01jabcdefghjkmnpqrstvwxyz0"


def test_build_batch_shape() -> None:
    body = payouts_l1.build_batch(
        RUN,
        [" SB-Sara@Personal.Example.com ", "sb-omar@personal.example.com"],
        Decimal("1.00"),
        "USD",
    )
    assert body["sender_batch_header"]["sender_batch_id"] == f"fd-l1-{RUN}"
    assert (
        body["sender_batch_header"]["email_subject"] == "Leash L1 sandbox payout test"
    )
    items = body["items"]
    assert len(items) == 2
    assert all(i["recipient_type"] == "EMAIL" for i in items)
    assert all(i["amount"] == {"value": "1.00", "currency": "USD"} for i in items)
    assert [i["sender_item_id"] for i in items] == [
        f"prl_l1_{RUN}_1",
        f"prl_l1_{RUN}_2",
    ]
    assert items[0]["receiver"] == "sb-sara@personal.example.com"
    assert [i["note"] for i in items] == [
        "Leash L1 test item 1",
        "Leash L1 test item 2",
    ]


def test_payout_request_id_deterministic_and_distinct() -> None:
    a = payouts_l1.payout_request_id(RUN, "payout")
    assert a == payouts_l1.payout_request_id(RUN, "payout")
    assert a != payouts_l1.payout_request_id(RUN, "payout-resend")
    uuid.UUID(a)


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        ("sara@x.com", "sa***@x.com"),
        ("sb-member@personal.example.com", "sb***@personal.example.com"),
        ("ab@x.com", "*@x.com"),
        ("a@x.com", "*@x.com"),
    ],
)
def test_mask_email(email: str, masked: str) -> None:
    assert payouts_l1.mask_email(email) == masked


def test_redact_replaces_recipient_emails_everywhere() -> None:
    recipients = ["sb-Sara@Personal.Example.com", "sb-omar@personal.example.com"]
    payload: dict[str, Any] = {
        "a": "to SB-SARA@personal.example.com now",
        "b": [{"receiver": "SB-Omar@Personal.Example.com"}, 5, None],
        "c": {"x": {"y": "sb-sara@personal.example.com"}},
    }
    snapshot = json.dumps(payload)
    result = payouts_l1.redact(payload, recipients)
    dumped = json.dumps(result).lower()
    assert "sb-sara@" not in dumped
    assert "sb-omar@" not in dumped
    assert "sb-member1@personal.example.com" in dumped
    assert "sb-member2@personal.example.com" in dumped
    assert json.dumps(payload) == snapshot


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        ({"batch_header": {"batch_status": "PROCESSING"}}, False),
        (
            {
                "batch_header": {"batch_status": "SUCCESS"},
                "items": [{"transaction_status": "PENDING"}],
            },
            False,
        ),
        (
            {
                "batch_header": {"batch_status": "SUCCESS"},
                "items": [
                    {"transaction_status": "SUCCESS"},
                    {"transaction_status": "UNCLAIMED"},
                ],
            },
            True,
        ),
        ({"batch_header": {"batch_status": "DENIED"}}, True),
    ],
)
def test_is_batch_terminal(batch: dict[str, Any], expected: bool) -> None:
    assert payouts_l1.is_batch_terminal(batch) is expected


@pytest.mark.parametrize("text", ["1", "1.0", "0.00", "-1.00", "1e2", " 1.00"])
def test_parse_amount_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        payouts_l1.parse_amount(text)


def test_parse_amount_accepts() -> None:
    assert payouts_l1.parse_amount("1.00") == Decimal("1.00")

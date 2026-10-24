"""Tests for scripts/payouts_l1.py (Payouts L1 sandbox proof). PayPal is always mocked."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

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


# ---------------------------------------------------------------------------
# Task 2: client, L1 flow, CLI (all PayPal traffic mocked with respx)
# ---------------------------------------------------------------------------

BASE = "https://api-m.sandbox.paypal.com"
RECIPIENTS = ["sb-sara@personal.example.com", "sb-omar@personal.example.com"]
BATCH_ID = "5UXD2E8A7EBQJ"
ITEM_IDS = [f"prl_l1_{RUN}_1", f"prl_l1_{RUN}_2"]


class FakeAuth:
    def __init__(self) -> None:
        self.tokens = 0
        self.invalidations = 0

    async def get_access_token(self) -> str:
        self.tokens += 1
        return "test-token"

    def invalidate(self) -> None:
        self.invalidations += 1


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _created() -> dict[str, Any]:
    return {"batch_header": {"payout_batch_id": BATCH_ID, "batch_status": "PENDING"}}


def _batch(status: str, item_status: str = "SUCCESS", count: int = 2) -> dict[str, Any]:
    return {
        "batch_header": {"payout_batch_id": BATCH_ID, "batch_status": status},
        "items": [
            {
                "payout_item_id": f"ITEM{n}",
                "transaction_status": item_status,
                "payout_item": {
                    "receiver": RECIPIENTS[n - 1],
                    "sender_item_id": ITEM_IDS[n - 1],
                },
            }
            for n in range(1, count + 1)
        ],
    }


DUP_BODY = {
    "name": "USER_BUSINESS_ERROR",
    "details": [{"issue": "Batch with given sender_batch_id already exists"}],
}


def _make(
    sleeper: Sleeper | None = None, auth: FakeAuth | None = None
) -> tuple[payouts_l1.PayoutsClient, FakeAuth, Sleeper, httpx.AsyncClient]:
    auth = auth or FakeAuth()
    sleeper = sleeper or Sleeper()
    http = httpx.AsyncClient()
    return (
        payouts_l1.PayoutsClient(BASE, auth, http, sleep=sleeper),
        auth,
        sleeper,
        http,
    )


def _mock_happy(router: respx.MockRouter) -> respx.Route:
    post = router.post("/v1/payments/payouts").mock(
        side_effect=[
            httpx.Response(201, json=_created()),
            httpx.Response(400, json=DUP_BODY),
        ]
    )
    router.get(f"/v1/payments/payouts/{BATCH_ID}").mock(
        side_effect=[
            httpx.Response(200, json=_batch("PROCESSING", "PENDING")),
            httpx.Response(200, json=_batch("SUCCESS")),
        ]
    )
    router.get("/v1/payments/payouts-item/ITEM1").mock(
        return_value=httpx.Response(200, json={"payout_item_id": "ITEM1"})
    )
    router.get("/v1/notifications/webhooks-events").mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {
                        "id": "WH-1",
                        "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
                        "resource": {"payout_batch_id": BATCH_ID},
                    }
                ]
            },
        )
    )
    return post


def _check(result: payouts_l1.L1Result, name: str) -> payouts_l1.Check:
    return next(c for c in result.checks if c.name == name)


@pytest.mark.asyncio
async def test_happy_path_passes_all_checks() -> None:
    client, _, sleeper, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        post = _mock_happy(router)
        result = await payouts_l1.run_l1(
            client, RECIPIENTS, Decimal("1.00"), "USD", RUN
        )
    assert result.ok, [c for c in result.checks if not c.passed]
    assert sleeper.calls == [10]
    first, second = (c.request.headers["paypal-request-id"] for c in post.calls)
    assert first != second
    assert first == payouts_l1.payout_request_id(RUN, "payout")
    assert second == payouts_l1.payout_request_id(RUN, "payout-resend")
    assert set(result.payloads) == {
        "batch_create_201",
        "batch_final",
        "item",
        "duplicate_rejected",
        "webhook_item_succeeded",
    }


@pytest.mark.asyncio
async def test_resend_accepted_fails_double_pay_check() -> None:
    client, _, _, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        _mock_happy(router).mock(
            side_effect=[
                httpx.Response(201, json=_created()),
                httpx.Response(
                    201,
                    json={
                        "batch_header": {
                            "payout_batch_id": "SECONDBATCH",
                            "batch_status": "PENDING",
                        }
                    },
                ),
            ]
        )
        result = await payouts_l1.run_l1(
            client, RECIPIENTS, Decimal("1.00"), "USD", RUN
        )
    dup = _check(result, "duplicate_batch_rejected")
    assert not dup.passed
    assert "DOUBLE PAY" in dup.detail
    assert "SECONDBATCH" in dup.detail
    assert "duplicate_accepted_201" in result.payloads
    assert "duplicate_rejected" not in result.payloads
    assert not result.ok


@pytest.mark.asyncio
async def test_poll_stops_after_120_seconds() -> None:
    client, _, sleeper, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        post = _mock_happy(router)
        router.get(f"/v1/payments/payouts/{BATCH_ID}").mock(
            return_value=httpx.Response(200, json=_batch("PROCESSING", "PENDING"))
        )
        result = await payouts_l1.run_l1(
            client, RECIPIENTS, Decimal("1.00"), "USD", RUN
        )
    assert sleeper.calls == [10] * 12
    assert not _check(result, "batch_success").passed
    assert "PROCESSING" in _check(result, "batch_success").detail
    assert post.call_count == 2  # the resend is still attempted


@pytest.mark.asyncio
async def test_unclaimed_item_is_terminal_and_fails() -> None:
    client, _, sleeper, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        _mock_happy(router)
        router.get(f"/v1/payments/payouts/{BATCH_ID}").mock(
            return_value=httpx.Response(200, json=_batch("SUCCESS", "UNCLAIMED"))
        )
        result = await payouts_l1.run_l1(
            client, RECIPIENTS, Decimal("1.00"), "USD", RUN
        )
    assert sleeper.calls == []
    items = _check(result, "items_success")
    assert not items.passed
    assert "UNCLAIMED" in items.detail
    assert "sb-sara@personal.example.com" not in items.detail


@pytest.mark.asyncio
async def test_401_refreshes_token_once() -> None:
    client, auth, _, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        post = router.post("/v1/payments/payouts").mock(
            side_effect=[
                httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"}),
                httpx.Response(201, json=_created()),
            ]
        )
        response = await client.create_batch({"x": 1}, "req-1")
    assert response.status_code == 201
    assert auth.invalidations == 1
    assert post.call_count == 2
    assert {c.request.headers["paypal-request-id"] for c in post.calls} == {"req-1"}


@pytest.mark.asyncio
async def test_5xx_retries_with_same_request_id() -> None:
    client, _, sleeper, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        post = router.post("/v1/payments/payouts").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(201, json=_created()),
            ]
        )
        response = await client.create_batch({"x": 1}, "req-2")
    assert response.status_code == 201
    assert sleeper.calls == [1, 3]
    assert {c.request.headers["paypal-request-id"] for c in post.calls} == {"req-2"}


@pytest.mark.asyncio
async def test_timeout_retries_then_raises() -> None:
    client, _, sleeper, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        post = router.post("/v1/payments/payouts").mock(
            side_effect=httpx.ReadTimeout("slow")
        )
        with pytest.raises(httpx.ReadTimeout):
            await client.create_batch({"x": 1}, "req-3")
    assert post.call_count == 3
    assert sleeper.calls == [1, 3]


@pytest.mark.asyncio
async def test_400_is_not_retried() -> None:
    client, _, sleeper, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        post = router.post("/v1/payments/payouts").mock(
            return_value=httpx.Response(400, json={"name": "VALIDATION_ERROR"})
        )
        result = await payouts_l1.run_l1(
            client, RECIPIENTS, Decimal("1.00"), "USD", RUN
        )
    assert post.call_count == 1
    assert sleeper.calls == []
    assert not result.ok
    assert not _check(result, "batch_created").passed
    assert result.payloads == {}


@pytest.mark.asyncio
async def test_webhook_lookup_failure_is_not_fatal() -> None:
    client, _, _, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        _mock_happy(router)
        router.get("/v1/notifications/webhooks-events").mock(
            return_value=httpx.Response(403, json={"name": "NOT_AUTHORIZED"})
        )
        result = await payouts_l1.run_l1(
            client, RECIPIENTS, Decimal("1.00"), "USD", RUN
        )
    assert result.ok
    assert "webhook_item_succeeded" not in result.payloads


@pytest.mark.asyncio
async def test_cli_rejects_wrong_recipient_count_and_bad_amount() -> None:
    with pytest.raises(SystemExit) as one_to:
        await payouts_l1.main(["--to", "a@x.com"])
    assert one_to.value.code == 2
    with pytest.raises(SystemExit) as bad_amount:
        await payouts_l1.main(["--to", "a@x.com", "--to", "b@x.com", "--amount", "1"])
    assert bad_amount.value.code == 2


def _patch_main(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSettings:
        paypal_api_base_url = BASE

    monkeypatch.setattr(payouts_l1, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(payouts_l1, "get_auth_manager", lambda: FakeAuth())
    monkeypatch.setattr(payouts_l1, "ULID", lambda: RUN.upper())


@pytest.mark.asyncio
async def test_output_never_contains_full_email(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_main(monkeypatch)
    async with respx.mock(base_url=BASE) as router:
        _mock_happy(router)
        router.get(f"/v1/payments/payouts/{BATCH_ID}").mock(
            return_value=httpx.Response(200, json=_batch("SUCCESS"))
        )
        code = await payouts_l1.main(["--to", RECIPIENTS[0], "--to", RECIPIENTS[1]])
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS batch_created" in out
    assert f"sender_batch_id: fd-l1-{RUN}" in out
    assert BATCH_ID in out
    for email in RECIPIENTS:
        assert email not in out


@pytest.mark.asyncio
async def test_save_fixtures_writes_redacted_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_main(monkeypatch)
    monkeypatch.setattr(payouts_l1, "FIXTURE_DIR", tmp_path / "paypal")
    async with respx.mock(base_url=BASE) as router:
        _mock_happy(router)
        router.get(f"/v1/payments/payouts/{BATCH_ID}").mock(
            return_value=httpx.Response(200, json=_batch("SUCCESS"))
        )
        code = await payouts_l1.main(
            ["--to", RECIPIENTS[0], "--to", RECIPIENTS[1], "--save-fixtures"]
        )
    assert code == 0
    files = sorted((tmp_path / "paypal").glob("payouts_*.json"))
    assert {f.name for f in files} >= {
        "payouts_batch_final.json",
        "payouts_batch_create_201.json",
    }
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert not any(email in text for email in RECIPIENTS)
    final = (tmp_path / "paypal" / "payouts_batch_final.json").read_text(
        encoding="utf-8"
    )
    assert "sb-member1@personal.example.com" in final


# ---------------------------------------------------------------------------
# Fix round 1
# ---------------------------------------------------------------------------


def _mock_post_then_dup(router: respx.MockRouter) -> None:
    router.post("/v1/payments/payouts").mock(
        side_effect=[
            httpx.Response(201, json=_created()),
            httpx.Response(400, json=DUP_BODY),
        ]
    )
    router.get(f"/v1/payments/payouts/{BATCH_ID}").mock(
        return_value=httpx.Response(500)
    )
    router.get("/v1/notifications/webhooks-events").mock(
        return_value=httpx.Response(200, json={"events": []})
    )


@pytest.mark.asyncio
async def test_step_failure_after_create_keeps_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, _, _, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        _mock_post_then_dup(router)
        result = await payouts_l1.run_l1(
            client, RECIPIENTS, Decimal("1.00"), "USD", RUN
        )
    out = capsys.readouterr().out
    assert f"fd-l1-{RUN}" in out
    assert BATCH_ID in out
    for email in RECIPIENTS:
        assert email not in out
    assert not result.ok
    assert not _check(result, "batch_poll").passed
    assert _check(result, "duplicate_batch_rejected").passed
    assert "batch_create_201" in result.payloads


@pytest.mark.asyncio
async def test_failure_after_create_exits_1_via_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_main(monkeypatch)

    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr(payouts_l1.asyncio, "sleep", instant)
    async with respx.mock(base_url=BASE) as router:
        _mock_post_then_dup(router)
        code = await payouts_l1.main(["--to", RECIPIENTS[0], "--to", RECIPIENTS[1]])
    out = capsys.readouterr().out
    assert code == 1
    assert BATCH_ID in out
    assert f"fd-l1-{RUN}" in out
    assert "PASS duplicate_batch_rejected" in out


@pytest.mark.asyncio
async def test_first_post_timeouts_print_reconcile_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, _, _, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        router.post("/v1/payments/payouts").mock(side_effect=httpx.ReadTimeout("x"))
        result = await payouts_l1.run_l1(
            client, RECIPIENTS, Decimal("1.00"), "USD", RUN
        )
    out = capsys.readouterr().out
    assert "batch may exist at PayPal: reconcile sender_batch_id" in out
    assert f"fd-l1-{RUN}" in out
    assert not result.ok


@pytest.mark.asyncio
async def test_auth_error_prints_type_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from backend.app.services.paypal.auth import PayPalAuthError

    class BadAuth(FakeAuth):
        async def get_access_token(self) -> str:
            raise PayPalAuthError(401, "SECRET-DETAIL")

    class FakeSettings:
        paypal_api_base_url = BASE

    monkeypatch.setattr(payouts_l1, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(payouts_l1, "get_auth_manager", lambda: BadAuth())
    code = await payouts_l1.main(["--to", RECIPIENTS[0], "--to", RECIPIENTS[1]])
    out = capsys.readouterr().out
    assert code == 1
    assert "PayPalAuthError" in out
    assert "SECRET-DETAIL" not in out


@pytest.mark.asyncio
async def test_duplicate_rejected_for_other_reason_fails() -> None:
    client, _, _, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        _mock_happy(router).mock(
            side_effect=[
                httpx.Response(201, json=_created()),
                httpx.Response(
                    400,
                    json={
                        "name": "INSUFFICIENT_FUNDS",
                        "details": [{"issue": "Sender has insufficient funds"}],
                    },
                ),
            ]
        )
        result = await payouts_l1.run_l1(
            client, RECIPIENTS, Decimal("1.00"), "USD", RUN
        )
    dup = _check(result, "duplicate_batch_rejected")
    assert not dup.passed
    assert "rejected for another reason: INSUFFICIENT_FUNDS" in dup.detail


@pytest.mark.asyncio
async def test_webhook_event_must_match_this_batch() -> None:
    client, _, _, http = _make()
    other = {
        "id": "WH-OTHER",
        "resource": {
            "payout_batch_id": "OTHERBATCH",
            "payout_item": {"receiver": "stranger@real-domain.com"},
        },
    }
    mine = {"id": "WH-MINE", "resource": {"payout_batch_id": BATCH_ID}}
    async with http, respx.mock(base_url=BASE) as router:
        route = router.get("/v1/notifications/webhooks-events").mock(
            return_value=httpx.Response(200, json={"events": [other, mine]})
        )
        found = await client.latest_webhook_event("X", BATCH_ID)
        assert found == mine
        assert route.calls[0].request.url.params["page_size"] == "10"
        route.mock(return_value=httpx.Response(200, json={"events": [other]}))
        assert await client.latest_webhook_event("X", BATCH_ID) is None


def test_save_fixtures_backstop_removes_third_party_emails(tmp_path: Path) -> None:
    payload = {
        "a": f"to {RECIPIENTS[0]} and Third.Person+x@Real-Domain.co.uk",
        "b": [{"email": "payer@example.org"}],
    }
    payouts_l1.save_fixtures({"item": payload}, RECIPIENTS, tmp_path)
    text = (tmp_path / "payouts_item.json").read_text(encoding="utf-8")
    assert "real-domain" not in text.lower()
    assert "payer@example.org" not in text
    assert RECIPIENTS[0] not in text
    assert "redacted@example.com" in text
    assert "sb-member1@personal.example.com" in text


@pytest.mark.asyncio
async def test_cli_amount_cap_and_duplicate_recipients() -> None:
    with pytest.raises(SystemExit) as over:
        await payouts_l1.main(
            ["--to", "a@x.com", "--to", "b@x.com", "--amount", "10.01"]
        )
    assert over.value.code == 2
    with pytest.raises(SystemExit) as same:
        await payouts_l1.main(["--to", "a@x.com", "--to", " A@X.com "])
    assert same.value.code == 2


@pytest.mark.asyncio
async def test_422_is_not_retried() -> None:
    client, _, sleeper, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        post = router.post("/v1/payments/payouts").mock(
            return_value=httpx.Response(422, json={"name": "UNPROCESSABLE"})
        )
        response = await client.create_batch({"x": 1}, "req-4")
    assert response.status_code == 422
    assert post.call_count == 1
    assert sleeper.calls == []


@pytest.mark.asyncio
async def test_401_on_get_refreshes_once() -> None:
    client, auth, _, http = _make()
    async with http, respx.mock(base_url=BASE) as router:
        get = router.get(f"/v1/payments/payouts/{BATCH_ID}").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json=_batch("SUCCESS")),
            ]
        )
        body = await client.get_batch(BATCH_ID)
    assert body["batch_header"]["batch_status"] == "SUCCESS"
    assert auth.invalidations == 1
    assert get.call_count == 2

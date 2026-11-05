"""Payouts client and status sync: respx on the sandbox host, temp DB, fake token."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile

os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="fd-leash-"), "default.db"),
)
os.environ.setdefault("PAYPAL_CLIENT_ID", "test-client-id")
os.environ.setdefault("PAYPAL_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("DEMO_KEY", "test-demo-key-0123456789abcdef")
os.environ.setdefault("PAYPAL_API_BASE_URL", "https://api-m.sandbox.paypal.com")
os.environ.setdefault("PAYPAL_WEBHOOK_ID", "wh-test-id")

import uuid  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from backend.app.api.errors import ApiError  # noqa: E402
from backend.app.db import models as dm  # noqa: E402
from backend.app.db.database import Base  # noqa: E402
from backend.app.leash import models as lm  # noqa: E402
from backend.app.leash.payroll import runs  # noqa: E402
from backend.app.services.paypal import _client, payouts, payouts_sync  # noqa: E402
from backend.app.services.paypal._client import PayPalAPIError  # noqa: E402

D = Decimal
SessionMaker = Callable[[], Session]
FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "leash" / "paypal"
PAYOUTS_URL = "https://api-m.sandbox.paypal.com/v1/payments/payouts"
RUN_ID = "prn_test0001"
KEY = "send-key-1"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


class _FakeAuth:
    async def get_access_token(self) -> str:
        return "fake-token"

    def invalidate(self) -> None:
        return None


@pytest.fixture
def sf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SessionMaker]:
    eng = create_engine(f"sqlite:///{tmp_path / 'payouts.db'}", future=True)

    @event.listens_for(eng, "connect")
    def _pragma(conn: Any, _rec: Any) -> None:
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    monkeypatch.setattr(runs, "session_factory", factory)
    monkeypatch.setattr(_client, "get_auth_manager", lambda: _FakeAuth())
    monkeypatch.setattr(_client, "RETRY_DELAY_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(_client, "_http_client", None)
    runs._locks.clear()
    payouts_sync._tasks.clear()
    payouts_sync._pollers.clear()
    with factory() as db:
        db.add(
            dm.Contract(
                id="cnt_01",
                title="Website build",
                client_name="Acme",
                client_paypal_email="client@example.com",
                currency="USD",
                raw_text="x",
                penalty_clause_quote="q",
                extraction_confidence=D("0.90"),
                status="ACTIVE",
            )
        )
        db.flush()
        db.add(
            dm.Milestone(
                id="ms_01",
                contract_id="cnt_01",
                code="MS-01",
                title="Phase 1: Backend Architecture Delivery",
                expected_amount=D("1000.00"),
                due_date=date(2026, 11, 1),
                source_clause_quote="q",
            )
        )
        for mid, name, email, rate in (
            ("tm_sara", "Sara", "sb-sara@personal.example.com", "22.00"),
            ("tm_youssef", "Youssef", "sb-youssef@personal.example.com", "25.00"),
            ("tm_omar", "Omar", "sb-omar@personal.example.com", "30.00"),
        ):
            db.add(
                lm.TeamMemberRow(
                    id=mid, name=name, payout_email=email, hourly_rate=D(rate), active=True
                )
            )
        db.flush()
        db.add(
            lm.PayrollRunRow(
                id=RUN_ID,
                mode="MILESTONE",
                milestone_id="ms_01",
                status="CHECKED",
                total=D("420.00"),
                cash_in_snapshot=D("850.00"),
                cash_used_pct=D("49.41"),
            )
        )
        db.flush()
        for lid, mid, hours, rate, gross in (
            ("prl_0002", "tm_sara", "10.00", "22.00", "220.00"),
            ("prl_0003", "tm_youssef", "8.00", "25.00", "200.00"),
        ):
            db.add(
                lm.PayrollLineRow(
                    id=lid,
                    run_id=RUN_ID,
                    team_member_id=mid,
                    hours=D(hours),
                    rate=D(rate),
                    gross=D(gross),
                    decision="ALLOW",
                    flags_json=[],
                )
            )
        db.commit()
    yield factory
    eng.dispose()


def _lines() -> list[Any]:
    return runs.get_run_out(RUN_ID).lines


def _run(sf: SessionMaker) -> lm.PayrollRunRow:
    with sf() as db:
        row = db.get(lm.PayrollRunRow, RUN_ID)
        assert row is not None
        return row


def _line(sf: SessionMaker, line_id: str) -> lm.PayrollLineRow:
    with sf() as db:
        row = db.get(lm.PayrollLineRow, line_id)
        assert row is not None
        return row


def _created(batch_id: str = "5UXD2E8A7EBQJ") -> httpx.Response:
    body = _fixture("payouts_batch_create_201.json")
    body["batch_header"]["payout_batch_id"] = batch_id
    return httpx.Response(201, json=body)


def _send(key: str = KEY) -> str:
    return asyncio.run(payouts.send_payroll_batch(RUN_ID, _lines(), key))


# --- build_batch -------------------------------------------------------------------------------


def test_build_batch_matches_a23(sf: SessionMaker) -> None:
    with sf() as db:
        run = db.get(lm.PayrollRunRow, RUN_ID)
        milestone = db.get(dm.Milestone, "ms_01")
        assert run is not None and milestone is not None
        body = payouts.build_batch(run, _lines(), milestone)
    assert body == {
        "sender_batch_header": {
            "sender_batch_id": f"fd-ms01-{RUN_ID}",
            "email_subject": "You have a payment for milestone MS-01",
            "email_message": "Thank you for your work on Phase 1: Backend Architecture Delivery.",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": "sb-sara@personal.example.com",
                "amount": {"value": "220.00", "currency": "USD"},
                "note": "MS-01 · 10.00 h x $22.00",
                "sender_item_id": "prl_0002",
            },
            {
                "recipient_type": "EMAIL",
                "receiver": "sb-youssef@personal.example.com",
                "amount": {"value": "200.00", "currency": "USD"},
                "note": "MS-01 · 8.00 h x $25.00",
                "sender_item_id": "prl_0003",
            },
        ],
    }


# --- send ---------------------------------------------------------------------------------------


@respx.mock
def test_send_posts_one_batch_and_marks_sent(sf: SessionMaker) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    assert _send() == "5UXD2E8A7EBQJ"
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["PayPal-Request-Id"] == str(
        uuid.uuid5(uuid.NAMESPACE_DNS, f"{RUN_ID}:payout")
    )
    sent = json.loads(request.content)
    assert sent["sender_batch_header"]["sender_batch_id"] == f"fd-ms01-{RUN_ID}"
    run = _run(sf)
    assert run.status == "SENT"
    assert run.sender_batch_id == f"fd-ms01-{RUN_ID}"
    assert run.paypal_batch_id == "5UXD2E8A7EBQJ"
    assert run.batch_status == "PENDING"
    assert run.idempotency_key == KEY
    assert run.sent_at is not None
    assert _line(sf, "prl_0002").payout_status == "PENDING"


@respx.mock
def test_excluded_line_is_left_unsent(sf: SessionMaker) -> None:
    respx.post(PAYOUTS_URL).mock(return_value=_created())
    only_sara = [ln for ln in _lines() if ln.line_id == "prl_0002"]
    asyncio.run(payouts.send_payroll_batch(RUN_ID, only_sara, KEY))
    assert _line(sf, "prl_0002").payout_status == "PENDING"
    assert _line(sf, "prl_0003").payout_status is None


@respx.mock
def test_send_twice_posts_one_batch(sf: SessionMaker) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    assert _send() == "5UXD2E8A7EBQJ"
    assert _send() == "5UXD2E8A7EBQJ"
    assert route.call_count == 1


@respx.mock
def test_different_key_on_sent_run_is_409_without_post(sf: SessionMaker) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    _send()
    with pytest.raises(ApiError) as err:
        _send("another-key")
    assert err.value.status_code == 409 and err.value.code == "RUN_NOT_SENDABLE"
    assert route.call_count == 1


@respx.mock
def test_concurrent_sends_post_once(sf: SessionMaker) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())

    async def both() -> list[Any]:
        lines = _lines()
        return await asyncio.gather(
            payouts.send_payroll_batch(RUN_ID, lines, KEY),
            payouts.send_payroll_batch(RUN_ID, lines, KEY),
            payouts.send_payroll_batch(RUN_ID, lines, "other"),
            return_exceptions=True,
        )

    results = asyncio.run(both())
    assert route.call_count == 1
    assert results.count("5UXD2E8A7EBQJ") == 2
    assert sum(isinstance(r, ApiError) and r.code == "RUN_NOT_SENDABLE" for r in results) == 1


@respx.mock
def test_duplicate_batch_rejection_marks_unknown(sf: SessionMaker) -> None:
    route = respx.post(PAYOUTS_URL).mock(
        return_value=httpx.Response(400, json=_fixture("payouts_duplicate_rejected.json"))
    )
    with pytest.raises(ApiError) as err:
        _send()
    assert err.value.status_code == 409 and err.value.code == "RUN_NOT_SENDABLE"
    run = _run(sf)
    assert run.status == "SENT" and run.batch_status == "UNKNOWN"
    assert run.paypal_batch_id is None
    with pytest.raises(ApiError):
        _send()  # still no second POST
    assert route.call_count == 1


@respx.mock
def test_5xx_then_success_uses_same_request_id(sf: SessionMaker) -> None:
    route = respx.post(PAYOUTS_URL).mock(
        side_effect=[httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}), _created()]
    )
    assert _send() == "5UXD2E8A7EBQJ"
    assert route.call_count == 2
    ids = {c.request.headers["PayPal-Request-Id"] for c in route.calls}
    assert len(ids) == 1


@respx.mock
def test_unavailable_marks_unknown_and_503(sf: SessionMaker) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=httpx.Response(502, json={}))
    with pytest.raises(ApiError) as err:
        _send()
    assert err.value.status_code == 503 and err.value.code == "PAYPAL_UNAVAILABLE"
    run = _run(sf)
    assert run.status == "SENT" and run.batch_status == "UNKNOWN"
    assert route.call_count == 3
    with pytest.raises(ApiError) as again:
        _send()
    assert again.value.code == "RUN_NOT_SENDABLE"
    assert route.call_count == 3


@respx.mock
def test_validation_error_releases_reservation(sf: SessionMaker) -> None:
    respx.post(PAYOUTS_URL).mock(
        return_value=httpx.Response(
            422,
            json={"name": "VALIDATION_ERROR", "details": [{"issue": "Invalid receiver"}]},
        )
    )
    with pytest.raises(PayPalAPIError):
        _send()
    run = _run(sf)
    assert run.status == "CHECKED"
    assert run.sender_batch_id is None and run.idempotency_key is None
    assert _line(sf, "prl_0002").payout_status is None


def test_unknown_run_is_404(sf: SessionMaker) -> None:
    with pytest.raises(ApiError) as err:
        asyncio.run(payouts.send_payroll_batch("prn_missing", [], KEY))
    assert err.value.status_code == 404


# --- status sync ----------------------------------------------------------------------------------


def _sent_run(sf: SessionMaker, batch_id: str | None = "5UXD2E8A7EBQJ") -> None:
    with sf() as db:
        run = db.get(lm.PayrollRunRow, RUN_ID)
        assert run is not None
        run.status = "SENT"
        run.sender_batch_id = f"fd-ms01-{RUN_ID}"
        run.paypal_batch_id = batch_id
        run.batch_status = "PENDING" if batch_id else "UNKNOWN"
        for line in db.query(lm.PayrollLineRow).all():
            line.payout_status = "PENDING"
        db.commit()


@pytest.mark.parametrize(
    ("paypal", "ours"),
    [
        ("SUCCESS", "SUCCESS"),
        ("UNCLAIMED", "UNCLAIMED"),
        ("PENDING", "PENDING"),
        ("ONHOLD", "PENDING"),
        ("NEW", "PENDING"),
        ("FAILED", "FAILED"),
        ("RETURNED", "FAILED"),
        ("REFUNDED", "FAILED"),
        ("REVERSED", "FAILED"),
        ("BLOCKED", "FAILED"),
        ("DENIED", "FAILED"),
        ("CANCELED", "FAILED"),
    ],
)
def test_apply_item_status_maps_statuses(sf: SessionMaker, paypal: str, ours: str) -> None:
    _sent_run(sf)
    payouts_sync.apply_item_status("prl_0002", paypal, "ITEM1")
    line = _line(sf, "prl_0002")
    assert line.payout_status == ours
    assert line.paypal_transaction_status == paypal
    assert line.payout_item_id == "ITEM1"


def test_status_never_regresses(sf: SessionMaker) -> None:
    _sent_run(sf)
    payouts_sync.apply_item_status("prl_0002", "SUCCESS")
    payouts_sync.apply_item_status("prl_0002", "PENDING")
    payouts_sync.apply_item_status("prl_0002", "FAILED")
    assert _line(sf, "prl_0002").payout_status == "SUCCESS"
    payouts_sync.apply_item_status("prl_0003", "FAILED")
    payouts_sync.apply_item_status("prl_0003", "SUCCESS")
    assert _line(sf, "prl_0003").payout_status == "FAILED"


def test_unclaimed_may_move_forward_only(sf: SessionMaker) -> None:
    _sent_run(sf)
    payouts_sync.apply_item_status("prl_0002", "UNCLAIMED")
    payouts_sync.apply_item_status("prl_0002", "PENDING")
    assert _line(sf, "prl_0002").payout_status == "UNCLAIMED"
    payouts_sync.apply_item_status("prl_0002", "SUCCESS")
    assert _line(sf, "prl_0002").payout_status == "SUCCESS"
    payouts_sync.apply_item_status("prl_0003", "UNCLAIMED")
    payouts_sync.apply_item_status("prl_0003", "RETURNED")
    assert _line(sf, "prl_0003").payout_status == "FAILED"


def test_unknown_status_and_line_are_ignored(sf: SessionMaker) -> None:
    _sent_run(sf)
    payouts_sync.apply_item_status("prl_0002", "WEIRD")
    payouts_sync.apply_item_status("prl_nope", "SUCCESS")
    assert _line(sf, "prl_0002").payout_status == "PENDING"


def test_unsent_run_ignores_item_status(sf: SessionMaker) -> None:
    payouts_sync.apply_item_status("prl_0002", "SUCCESS")
    assert _line(sf, "prl_0002").payout_status is None


def _first_paid(sf: SessionMaker, member_id: str) -> Any:
    with sf() as db:
        member = db.get(lm.TeamMemberRow, member_id)
        assert member is not None
        return member.first_paid_at


def test_success_sets_first_paid_at_once(sf: SessionMaker) -> None:
    _sent_run(sf)
    assert _first_paid(sf, "tm_sara") is None
    payouts_sync.apply_item_status("prl_0002", "SUCCESS")
    first = _first_paid(sf, "tm_sara")
    assert first is not None
    assert _first_paid(sf, "tm_youssef") is None
    payouts_sync.apply_item_status("prl_0002", "SUCCESS")
    assert _first_paid(sf, "tm_sara") == first


def test_run_paid_when_all_success(sf: SessionMaker) -> None:
    _sent_run(sf)
    payouts_sync.apply_item_status("prl_0002", "SUCCESS")
    assert _run(sf).status == "SENT"
    payouts_sync.apply_item_status("prl_0003", "SUCCESS")
    assert _run(sf).status == "PAID"


def test_run_partial_when_mixed(sf: SessionMaker) -> None:
    _sent_run(sf)
    payouts_sync.apply_item_status("prl_0002", "SUCCESS")
    payouts_sync.apply_item_status("prl_0003", "FAILED")
    assert _run(sf).status == "PARTIAL"


def _item_event(sender_item_id: str, status: str = "SUCCESS") -> dict[str, Any]:
    resource = _fixture("payouts_item.json")
    resource["payout_item"]["sender_item_id"] = sender_item_id
    resource["transaction_status"] = status
    return {
        "id": "WH-1",
        "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
        "resource": resource,
    }


def test_handle_webhook_item_and_batch_denied(sf: SessionMaker) -> None:
    _sent_run(sf)
    asyncio.run(payouts_sync.handle_webhook_event(_item_event("prl_0002")))
    line = _line(sf, "prl_0002")
    assert line.payout_status == "SUCCESS" and line.payout_item_id == "XC4GNWZU7XB5S"
    denied = {
        "event_type": "PAYMENT.PAYOUTSBATCH.DENIED",
        "resource": {
            "batch_header": {
                "payout_batch_id": "5UXD2E8A7EBQJ",
                "batch_status": "DENIED",
            }
        },
    }
    asyncio.run(payouts_sync.handle_webhook_event(denied))
    run = _run(sf)
    assert run.batch_status == "DENIED" and run.status == "FAILED"
    assert _line(sf, "prl_0003").payout_status == "FAILED"
    assert _line(sf, "prl_0002").payout_status == "SUCCESS"  # a paid line stays paid


def test_webhook_batch_matches_unknown_run_by_sender_batch_id(sf: SessionMaker) -> None:
    _sent_run(sf, batch_id=None)
    event_ = {
        "event_type": "PAYMENT.PAYOUTSBATCH.PROCESSING",
        "resource": {
            "batch_header": {
                "payout_batch_id": "NEWBATCH123",
                "batch_status": "PROCESSING",
                "sender_batch_header": {"sender_batch_id": f"fd-ms01-{RUN_ID}"},
            }
        },
    }
    asyncio.run(payouts_sync.handle_webhook_event(event_))
    run = _run(sf)
    assert run.paypal_batch_id == "NEWBATCH123" and run.batch_status == "PROCESSING"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED"},
        {"event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "resource": {"payout_item": 3}},
        {"event_type": "PAYMENT.PAYOUTSBATCH.DENIED", "resource": {"batch_header": []}},
        {"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"x": 1}},
        {"event_type": 5, "resource": "nope"},
    ],
)
def test_malformed_webhooks_are_ignored(sf: SessionMaker, bad: dict[str, Any]) -> None:
    _sent_run(sf)
    asyncio.run(payouts_sync.handle_webhook_event(copy.deepcopy(bad)))
    assert _line(sf, "prl_0002").payout_status == "PENDING"


# --- polling ---------------------------------------------------------------------------------------


def _batch_body(status: str, item_status: str) -> dict[str, Any]:
    return {
        "batch_header": {"payout_batch_id": "5UXD2E8A7EBQJ", "batch_status": status},
        "items": [
            {
                "payout_item_id": f"IT{sid}",
                "transaction_status": item_status,
                "payout_item": {"sender_item_id": sid},
            }
            for sid in ("prl_0002", "prl_0003")
        ],
    }


@respx.mock
def test_poll_batch_stops_when_terminal(sf: SessionMaker) -> None:
    _sent_run(sf)
    route = respx.get(f"{PAYOUTS_URL}/5UXD2E8A7EBQJ").mock(
        side_effect=[
            httpx.Response(200, json=_batch_body("PENDING", "PENDING")),
            httpx.Response(200, json=_batch_body("SUCCESS", "SUCCESS")),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    asyncio.run(payouts_sync.poll_batch(RUN_ID, interval_s=10, max_s=120, sleep=fake_sleep))
    assert route.call_count == 2
    assert sleeps == [10]
    run = _run(sf)
    assert run.status == "PAID" and run.batch_status == "SUCCESS"


@respx.mock
def test_poll_batch_gives_up_after_max(sf: SessionMaker) -> None:
    _sent_run(sf)
    route = respx.get(f"{PAYOUTS_URL}/5UXD2E8A7EBQJ").mock(
        return_value=httpx.Response(200, json=_batch_body("PENDING", "PENDING"))
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    asyncio.run(payouts_sync.poll_batch(RUN_ID, interval_s=10, max_s=30, sleep=fake_sleep))
    assert sleeps == [10, 10, 10]
    assert route.call_count == 4
    assert _run(sf).status == "SENT"


@respx.mock
def test_poll_survives_paypal_errors(sf: SessionMaker) -> None:
    _sent_run(sf)
    respx.get(f"{PAYOUTS_URL}/5UXD2E8A7EBQJ").mock(
        side_effect=[
            httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"}),
            httpx.Response(200, json=_batch_body("SUCCESS", "SUCCESS")),
        ]
    )

    async def fake_sleep(_s: float) -> None:
        return None

    asyncio.run(payouts_sync.poll_batch(RUN_ID, interval_s=1, max_s=10, sleep=fake_sleep))
    assert _run(sf).status == "PAID"


def test_resume_pending_starts_for_sent_runs(
    sf: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    gate: list[asyncio.Event] = []

    async def fake_poll(run_id: str, **_kw: Any) -> None:
        started.append(run_id)
        await gate[0].wait()

    monkeypatch.setattr(payouts_sync, "poll_batch", fake_poll)
    _sent_run(sf)

    async def go() -> tuple[int, int]:
        gate.append(asyncio.Event())
        first = await payouts_sync.resume_pending()
        again = await payouts_sync.resume_pending()  # no second poller while one runs
        await asyncio.sleep(0)
        gate[0].set()
        await payouts_sync.wait_for_polling()
        return first, again

    first, again = asyncio.run(go())
    assert first == 1 and again == 1
    assert started == [RUN_ID]


def test_resume_pending_skips_unsent_and_unknown(sf: SessionMaker) -> None:
    assert asyncio.run(payouts_sync.resume_pending()) == 0
    _sent_run(sf, batch_id=None)
    assert asyncio.run(payouts_sync.resume_pending()) == 0


def test_start_polling_is_idempotent(sf: SessionMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    gate: list[asyncio.Event] = []

    async def slow_poll(run_id: str, **_kw: Any) -> None:
        calls.append(run_id)
        await gate[0].wait()

    monkeypatch.setattr(payouts_sync, "poll_batch", slow_poll)

    async def go() -> None:
        gate.append(asyncio.Event())
        t1 = payouts_sync.start_polling(RUN_ID)
        t2 = payouts_sync.start_polling(RUN_ID)
        assert t1 is t2
        await asyncio.sleep(0)
        gate[0].set()
        await payouts_sync.wait_for_polling()
        assert RUN_ID not in payouts_sync._pollers

    asyncio.run(go())
    assert calls == [RUN_ID]

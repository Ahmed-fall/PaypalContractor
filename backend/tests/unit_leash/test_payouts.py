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
from datetime import UTC, datetime  # noqa: E402
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
from backend.app.leash import service as service_module  # noqa: E402
from backend.app.leash.payroll import runs  # noqa: E402
from backend.app.leash.policy import default_policy  # noqa: E402
from backend.app.leash.service import LeashService, configure  # noqa: E402
from backend.app.leash.store import DEFAULT_POLICY_SOURCE, SqlDecisionLog, SqlPolicyProvider  # noqa: E402
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
    configure(LeashService(SqlPolicyProvider(factory), SqlDecisionLog(factory), explainer=None))
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
                    id=mid,
                    name=name,
                    payout_email=email,
                    hourly_rate=D(rate),
                    first_paid_at=datetime(2026, 9, 30, tzinfo=UTC),
                    active=True,
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
        policy = default_policy()
        now = datetime.now(UTC)
        db.add(
            lm.LeashPolicyRow(
                id="lpol_1",
                version=policy.version,
                source_text=DEFAULT_POLICY_SOURCE,
                compiled_json=policy.model_dump(mode="json"),
                active=True,
                created_at=now,
                activated_at=now,
            )
        )
        db.add(
            lm.TimesheetUploadRow(
                id="tsu_1",
                file_name="t.csv",
                file_hash="h" * 64,
                header_signature="sig",
                row_count=2,
                status="PARSED",
            )
        )
        db.flush()
        for eid, mid, name, hours in (
            ("tse_1", "tm_sara", "Sara", "10.00"),
            ("tse_2", "tm_youssef", "Youssef", "8.00"),
        ):
            db.add(
                lm.TimesheetEntryRow(
                    id=eid,
                    upload_id="tsu_1",
                    team_member_id=mid,
                    raw_member_name=name,
                    work_date=date(2026, 11, 2),
                    hours=D(hours),
                    milestone_code="MS-01",
                )
            )
        db.add(
            dm.PaypalCapture(
                id="cap_01",
                milestone_id="ms_01",
                paypal_order_id="ORDcap_01",
                paypal_capture_id="PPcap_01",
                gross_amount=D("850.00"),
                currency="USD",
                captured_at=now,
                source="sandbox",
                raw_payload_json={},
            )
        )
        db.commit()
    yield factory
    service_module._service = None
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
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    asyncio.run(runs.set_line_action(RUN_ID, "prl_0003", "EXCLUDED", None))
    ready = asyncio.run(runs.lines_ready_to_send(RUN_ID, []))
    assert [ln.line_id for ln in ready] == ["prl_0002"]
    asyncio.run(payouts.send_payroll_batch(RUN_ID, ready, KEY))
    assert [i["sender_item_id"] for i in json.loads(route.calls[0].request.content)["items"]] == [
        "prl_0002"
    ]
    assert _line(sf, "prl_0002").payout_status == "PENDING"
    assert _line(sf, "prl_0003").payout_status is None
    assert _run(sf).total == D("220.00")


@respx.mock
def test_expected_total_is_checked_under_the_run_lock(sf: SessionMaker) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    asyncio.run(runs.set_line_action(RUN_ID, "prl_0003", "EXCLUDED", None))
    ready = asyncio.run(runs.lines_ready_to_send(RUN_ID, []))
    with pytest.raises(ApiError) as err:
        asyncio.run(payouts.send_payroll_batch(RUN_ID, ready, KEY, expected_total=D("219.99")))
    assert err.value.status_code == 409 and err.value.code == "RUN_NOT_SENDABLE"
    assert route.call_count == 0
    assert _run(sf).sender_batch_id is None
    asyncio.run(payouts.send_payroll_batch(RUN_ID, ready, KEY, expected_total=D("220.00")))
    assert route.call_count == 1


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
    with sf() as db:
        for member in db.query(lm.TeamMemberRow):
            member.first_paid_at = None
        db.commit()
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
    clock = [0.0]

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    asyncio.run(
        payouts_sync.poll_batch(
            RUN_ID, interval_s=10, max_s=30, sleep=fake_sleep, clock=lambda: clock[0]
        )
    )
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


@respx.mock
def test_poll_deadline_uses_wall_clock_not_sum_of_sleeps(sf: SessionMaker) -> None:
    _sent_run(sf)
    route = respx.get(f"{PAYOUTS_URL}/5UXD2E8A7EBQJ").mock(
        return_value=httpx.Response(200, json=_batch_body("PENDING", "PENDING"))
    )
    clock = [0.0]

    async def slow_sleep(_seconds: float) -> None:
        clock[0] += 25.0  # each cycle really takes 25 s although the interval is 10 s

    asyncio.run(
        payouts_sync.poll_batch(
            RUN_ID, interval_s=10, max_s=30, sleep=slow_sleep, clock=lambda: clock[0]
        )
    )
    assert route.call_count == 3  # t=0, 25, 50 -> stop; summed sleeps (10, 20, 30) would add one


# --- fix round 1: the send re-checks under the lock -------------------------------------------


@respx.mock
def test_stale_excluded_line_is_not_paid(sf: SessionMaker) -> None:
    stale = _lines()  # what lines_ready_to_send returned before the owner acted
    with sf() as db:
        row = db.get(lm.PayrollLineRow, "prl_0003")
        assert row is not None
        row.owner_action = "EXCLUDED"
        db.commit()
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    with pytest.raises(ApiError) as err:
        asyncio.run(payouts.send_payroll_batch(RUN_ID, stale, "k"))
    assert err.value.status_code == 409 and err.value.code == "RUN_NOT_SENDABLE"
    assert route.call_count == 0
    assert _run(sf).sender_batch_id is None


@respx.mock
def test_stale_line_that_is_now_denied_is_not_paid(sf: SessionMaker) -> None:
    stale = _lines()
    with sf() as db:  # Sara's hours disappear: the re-check denies her line (PR-05)
        for entry in db.query(lm.TimesheetEntryRow).filter_by(team_member_id="tm_sara"):
            db.delete(entry)
        db.commit()
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    with pytest.raises(ApiError) as err:
        asyncio.run(payouts.send_payroll_batch(RUN_ID, stale, "k"))
    assert err.value.status_code == 409 and err.value.code == "RUN_NOT_SENDABLE"
    assert route.call_count == 0


@respx.mock
def test_foreign_line_id_is_not_posted(sf: SessionMaker) -> None:
    forged = _lines()[0].model_copy(update={"line_id": "prl_other_run"})
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    with pytest.raises(ApiError) as err:
        asyncio.run(payouts.send_payroll_batch(RUN_ID, [forged], "k"))
    assert err.value.status_code == 409 and err.value.code == "RUN_NOT_SENDABLE"
    assert route.call_count == 0


@respx.mock
def test_matching_set_sends_from_database_values(sf: SessionMaker) -> None:
    lines = _lines()
    tampered = [ln.model_copy(update={"gross": D("999.00")}) for ln in lines]
    with sf() as db:
        member = db.get(lm.TeamMemberRow, "tm_sara")
        assert member is not None
        member.payout_email = "sb-sara-new@personal.example.com"
        db.commit()
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    asyncio.run(payouts.send_payroll_batch(RUN_ID, tampered, "k"))
    items = json.loads(route.calls[0].request.content)["items"]
    assert [(i["sender_item_id"], i["amount"]["value"]) for i in items] == [
        ("prl_0002", "220.00"),
        ("prl_0003", "200.00"),
    ]
    assert items[0]["receiver"] == "sb-sara-new@personal.example.com"
    assert _run(sf).total == D("420.00")


# --- fix round 1: terminal batch status ----------------------------------------------------------


def _batch_event(status: str) -> dict[str, Any]:
    return {
        "event_type": f"PAYMENT.PAYOUTSBATCH.{status}",
        "resource": {"batch_header": {"payout_batch_id": "5UXD2E8A7EBQJ", "batch_status": status}},
    }


def test_denied_batch_is_not_regressed_by_late_event(sf: SessionMaker) -> None:
    _sent_run(sf)
    asyncio.run(payouts_sync.handle_webhook_event(_batch_event("DENIED")))
    assert _run(sf).status == "FAILED"
    asyncio.run(payouts_sync.handle_webhook_event(_batch_event("PROCESSING")))
    run = _run(sf)
    assert run.status == "FAILED" and run.batch_status == "DENIED"


def test_paid_run_batch_status_is_not_regressed(sf: SessionMaker) -> None:
    _sent_run(sf)
    payouts_sync.apply_item_status("prl_0002", "SUCCESS")
    payouts_sync.apply_item_status("prl_0003", "SUCCESS")
    asyncio.run(payouts_sync.handle_webhook_event(_batch_event("SUCCESS")))
    asyncio.run(payouts_sync.handle_webhook_event(_batch_event("PROCESSING")))
    run = _run(sf)
    assert run.status == "PAID" and run.batch_status == "SUCCESS"


def test_unknown_batch_status_may_move_to_anything(sf: SessionMaker) -> None:
    _sent_run(sf)
    with sf() as db:
        row = db.get(lm.PayrollRunRow, RUN_ID)
        assert row is not None
        row.batch_status = "UNKNOWN"
        db.commit()
    asyncio.run(payouts_sync.handle_webhook_event(_batch_event("PROCESSING")))
    assert _run(sf).batch_status == "PROCESSING"


# --- fix round 1: webhooks that beat our bookkeeping, and batch id reconciliation ----------------


@respx.mock
def test_webhook_between_201_and_mark_sent_is_applied(
    sf: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def post_then_webhook(_request: httpx.Request) -> httpx.Response:
        # The item webhook arrives before send_payroll_batch records the 201.
        payouts_sync.apply_item_status("prl_0002", "SUCCESS", "ITEM1", "5UXD2E8A7EBQJ")
        return _created()

    respx.post(PAYOUTS_URL).mock(side_effect=post_then_webhook)
    _send()
    assert _line(sf, "prl_0002").payout_status == "SUCCESS"
    assert _line(sf, "prl_0003").payout_status == "PENDING"
    assert _run(sf).status == "SENT"


def test_webhook_for_line_outside_the_batch_is_ignored(sf: SessionMaker) -> None:
    _sent_run(sf)
    with sf() as db:
        row = db.get(lm.PayrollLineRow, "prl_0003")
        assert row is not None
        row.payout_status = None  # excluded: never part of the batch
        db.commit()
    payouts_sync.apply_item_status("prl_0003", "SUCCESS")
    assert _line(sf, "prl_0003").payout_status is None


def test_item_webhook_fills_batch_id_of_unknown_run(sf: SessionMaker) -> None:
    _sent_run(sf, batch_id=None)
    asyncio.run(payouts_sync.handle_webhook_event(_item_event("prl_0002")))
    run = _run(sf)
    assert run.paypal_batch_id == "TNVNLATJ857QW"  # resource.payout_batch_id of the fixture
    assert _line(sf, "prl_0002").payout_status == "SUCCESS"


# --- final-review fixes ------------------------------------------------------------------------


def test_status_write_is_conditional_against_a_concurrent_terminal_write(
    sf: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sent_run(sf)
    real_allowed = payouts_sync._allowed

    def racing_allowed(current: str | None, new: str) -> bool:
        ok = real_allowed(current, new)
        with sf() as other:  # another writer lands SUCCESS after our read
            row = other.get(lm.PayrollLineRow, "prl_0002")
            assert row is not None
            row.payout_status = "SUCCESS"
            other.commit()
        return ok

    monkeypatch.setattr(payouts_sync, "_allowed", racing_allowed)
    payouts_sync.apply_item_status("prl_0002", "FAILED")
    assert _line(sf, "prl_0002").payout_status == "SUCCESS"


@respx.mock
def test_poll_survives_a_busy_database(sf: SessionMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.exc import OperationalError

    _sent_run(sf)
    respx.get(f"{PAYOUTS_URL}/5UXD2E8A7EBQJ").mock(
        return_value=httpx.Response(200, json=_batch_body("SUCCESS", "SUCCESS"))
    )
    real = payouts_sync._apply_batch_items
    calls = {"n": 0}

    def busy_once(items: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        real(items)

    monkeypatch.setattr(payouts_sync, "_apply_batch_items", busy_once)

    async def fake_sleep(_s: float) -> None:
        return None

    asyncio.run(payouts_sync.poll_batch(RUN_ID, interval_s=1, max_s=10, sleep=fake_sleep))
    assert calls["n"] == 2
    assert _run(sf).status == "PAID"


@respx.mock
def test_cancel_during_reservation_leaves_the_run_unreserved(
    sf: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    real_reserve = payouts._reserve

    started = threading.Event()

    def slow_reserve(*args: Any, **kwargs: Any) -> Any:
        started.set()
        time.sleep(0.3)
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(payouts, "_reserve", slow_reserve)

    async def go() -> None:
        task = asyncio.ensure_future(payouts.send_payroll_batch(RUN_ID, _lines(), KEY))
        while not started.is_set():  # cancel only once the reservation is running
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert route.call_count == 0
    run = _run(sf)
    assert run.sender_batch_id is None and run.idempotency_key is None
    assert run.status == "CHECKED"
    assert _line(sf, "prl_0002").payout_status is None
    # and it can still be sent afterwards
    assert _send("retry-key") == "5UXD2E8A7EBQJ"
    assert route.call_count == 1


def test_margin_pending_counts_only_pending_and_open_invoices_use_contract_currency(
    sf: SessionMaker,
) -> None:
    _sent_run(sf)
    with sf() as db:
        db.get(lm.PayrollLineRow, "prl_0002").payout_status = "UNCLAIMED"  # type: ignore[union-attr]
        db.add(
            dm.Discrepancy(
                id="dsc_1",
                milestone_id="ms_01",
                capture_id="cap_01",
                discrepancy_type="SHORT_PAY",
                shortfall_amount=D("150.00"),
                penalty_amount=D("0.00"),
                total_remediation_amount=D("150.00"),
                ai_audit_summary="s",
            )
        )
        db.flush()
        for n, (cur, amount) in enumerate((("USD", "150.00"), ("EUR", "999.00"))):
            db.add(
                dm.RemediationInvoice(
                    id=f"inv_{n}",
                    discrepancy_id="dsc_1",
                    attempt=n + 1,
                    paypal_invoice_id=f"INV2-{n}",
                    paypal_invoice_number=f"FD-{n}",
                    recipient_email="client@example.com",
                    total_amount=D(amount),
                    currency=cur,
                    status="SENT",
                    paypal_request_id=f"req-{n}",
                )
            )
        db.commit()
        row = runs.margin_rows(db).rows[0]
    assert row.pending_to_team == D("200.00")  # Youssef PENDING only; Sara is UNCLAIMED
    assert row.invoiced_open == D("150.00")  # the EUR invoice is not in this USD contract

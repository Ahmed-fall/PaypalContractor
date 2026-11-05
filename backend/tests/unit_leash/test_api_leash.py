"""Leash REST API (A.2.2) on a minimal app: temp DB, respx for Payouts, no real PayPal."""

from __future__ import annotations

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

import asyncio  # noqa: E402
import json  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from backend.app.api import deps  # noqa: E402
from backend.app.api import leash as leash_api  # noqa: E402
from backend.app.api.errors import RequestIdMiddleware, install_error_handlers  # noqa: E402
from backend.app.config import get_settings  # noqa: E402
from backend.app.db import models as dm  # noqa: E402
from backend.app.db.database import Base, get_db  # noqa: E402
from backend.app.leash import models as lm  # noqa: E402
from backend.app.leash import service as service_module  # noqa: E402
from backend.app.leash.payroll import runs  # noqa: E402
from backend.app.schemas.contracts import GuardAction, TeamMemberUpdate  # noqa: E402
from backend.app.leash.seed import seed_leash_demo  # noqa: E402
from backend.app.leash.service import LeashService, configure  # noqa: E402
from backend.app.leash.store import SqlDecisionLog, SqlPolicyProvider  # noqa: E402
from backend.app.services.paypal import _client, payouts_sync  # noqa: E402

D = Decimal
SessionMaker = Callable[[], Session]
FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "leash" / "paypal"
PAYOUTS_URL = "https://api-m.sandbox.paypal.com/v1/payments/payouts"
NOW = datetime.now(UTC)
PREFIX = "/api/v1/leash"


class _FakeAuth:
    async def get_access_token(self) -> str:
        return "fake-token"

    def invalidate(self) -> None:
        return None


class Ctx:
    def __init__(self, client: TestClient, sf: SessionMaker, polled: list[str]) -> None:
        self.client = client
        self.sf = sf
        self.polled = polled
        self.headers = {"X-Demo-Key": get_settings().demo_key.get_secret_value()}

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.client.get(PREFIX + path, **kw)

    def post(self, path: str, *, auth: bool = True, **kw: Any) -> httpx.Response:
        return self.client.post(PREFIX + path, headers=self.headers if auth else {}, **kw)

    def patch(self, path: str, **kw: Any) -> httpx.Response:
        return self.client.patch(PREFIX + path, headers=self.headers, **kw)


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Ctx]:
    eng: Engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _pragma(conn: Any, _rec: Any) -> None:
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    monkeypatch.setattr(runs, "session_factory", factory)
    monkeypatch.setattr(runs, "_load_mapper", lambda: None)
    monkeypatch.setattr(_client, "get_auth_manager", lambda: _FakeAuth())
    monkeypatch.setattr(_client, "RETRY_DELAY_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(_client, "_http_client", None)
    runs._locks.clear()
    deps._WRITE_LIMITS.clear()
    configure(LeashService(SqlPolicyProvider(factory), SqlDecisionLog(factory), explainer=None))

    polled: list[str] = []

    def _fake_start(run_id: str) -> None:
        polled.append(run_id)

    async def _fake_resume() -> int:
        polled.append("resume")
        return 0

    monkeypatch.setattr(payouts_sync, "start_polling", _fake_start)
    monkeypatch.setattr(payouts_sync, "resume_pending", _fake_resume)
    monkeypatch.setattr(leash_api, "_resumed", False)
    monkeypatch.setattr(leash_api, "_seeded", False)
    monkeypatch.setattr(leash_api, "_tables_ready", False)

    with factory() as db:
        seed_leash_demo(db, now=NOW)
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
        db.add(
            dm.PaypalCapture(
                id="cap_01",
                milestone_id="ms_01",
                paypal_order_id="ORDcap_01",
                paypal_capture_id="PPcap_01",
                gross_amount=D("850.00"),
                currency="USD",
                captured_at=NOW,
                source="sandbox",
                raw_payload_json={},
            )
        )
        db.commit()

    def _get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(leash_api.router)
    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as client:
        yield Ctx(client, factory, polled)
    service_module._service = None
    eng.dispose()


def _create_run(c: Ctx) -> dict[str, Any]:
    r = c.post("/payroll/runs", json={"mode": "MILESTONE", "milestone_id": "ms_01"})
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def _line(run: dict[str, Any], name: str) -> dict[str, Any]:
    return next(ln for ln in run["lines"] if ln["member_name"] == name)


def _created(batch_id: str = "5UXD2E8A7EBQJ") -> httpx.Response:
    body = json.loads((FIXTURES / "payouts_batch_create_201.json").read_text(encoding="utf-8"))
    body["batch_header"]["payout_batch_id"] = batch_id
    return httpx.Response(201, json=body)


# --- auth ---------------------------------------------------------------------------------


def test_writes_need_demo_key(ctx: Ctx) -> None:
    attempts = [
        ctx.post("/team", auth=False, json={"name": "X"}),
        ctx.client.patch(f"{PREFIX}/team/tm_x", json={}),
        ctx.post("/policy", auth=False, json={}),
        ctx.post("/policy/activate", auth=False, json={"version": 1}),
        ctx.post("/policy/compile", auth=False, json={"text": "x"}),
        ctx.post("/payroll/runs", auth=False, json={}),
        ctx.post("/payroll/runs/prn_x/send", auth=False, json={}),
        ctx.post("/timesheets", auth=False, files={"file": ("t.csv", b"a,b\n1,2\n")}),
    ]
    for r in attempts:
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "DEMO_KEY_REQUIRED"
    assert ctx.get("/team").status_code == 200  # reads are open


def test_resume_and_tables_run_once(ctx: Ctx) -> None:
    ctx.get("/team")
    ctx.get("/policy")
    ctx.get("/decisions")
    assert ctx.polled.count("resume") == 1


def test_seed_once_when_enabled(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    with ctx.sf() as db:
        for row in db.scalars(select(lm.TimesheetEntryRow)).all():
            db.delete(row)
        for row in db.scalars(select(lm.TimesheetUploadRow)).all():
            db.delete(row)
        for row in db.scalars(select(lm.TeamMemberRow)).all():
            db.delete(row)
        db.commit()
    monkeypatch.setattr(get_settings(), "seed_demo_data", True)
    r = ctx.get("/team")
    assert [m["name"] for m in r.json()] == ["Omar", "Sara", "Youssef"]


# --- team ---------------------------------------------------------------------------------


def test_team_crud_and_email_change_sets_timestamp(ctx: Ctx) -> None:
    created = ctx.post(
        "/team",
        json={"name": "Nadia", "payout_email": "Nadia@Example.com", "hourly_rate": "30.00"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["payout_email"] == "nadia@example.com"
    assert body["email_changed_at"] is None
    assert body["active"] is True
    mid = body["member_id"]

    renamed = ctx.patch(f"/team/{mid}", json={"hourly_rate": "31.00"})
    assert renamed.status_code == 200
    assert renamed.json()["email_changed_at"] is None

    changed = ctx.patch(f"/team/{mid}", json={"payout_email": "nadia.new@example.com"})
    assert changed.status_code == 200
    assert changed.json()["email_changed_at"] is not None

    names = [m["name"] for m in ctx.get("/team").json()]
    assert "Nadia" in names and "Omar" in names

    dup = ctx.post(
        "/team",
        json={"name": "Other", "payout_email": "nadia.new@example.com", "hourly_rate": "20.00"},
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "PAYOUT_EMAIL_IN_USE"


# --- timesheets ---------------------------------------------------------------------------


def test_timesheet_upload_flow(ctx: Ctx) -> None:
    known = b"Employee,Day,Hrs,Project\nSara,2026-11-05,2,MS-01\n"
    r = ctx.post("/timesheets", files={"file": ("known.csv", known, "text/csv")})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "PARSED"
    assert r.json()["entries"] == 1
    assert r.json()["milestones"] == ["MS-01"]

    again = ctx.post("/timesheets", files={"file": ("known.csv", known, "text/csv")})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "DUPLICATE_UPLOAD"

    odd = b"Staff,Date,Hours worked,Job\nSara,2026-11-05,3,MS-01\nOmar,2026-11-05,1,MS-01\n"
    pending = ctx.post("/timesheets", files={"file": ("odd.csv", odd, "text/csv")})
    assert pending.status_code == 201, pending.text
    body = pending.json()
    assert body["status"] == "MAPPING_REQUIRED"
    assert body["headers"] == ["Staff", "Date", "Hours worked", "Job"]
    assert set(body["proposal"]) == {"mapping", "confidence"}

    blocked = ctx.post("/payroll/runs", json={"mode": "MILESTONE", "milestone_id": "ms_01"})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "MAPPING_REQUIRED"

    mapping = {
        "Staff": "member",
        "Date": "work_date",
        "Hours worked": "hours",
        "Job": "milestone_code",
    }
    done = ctx.post(f"/timesheets/{body['upload_id']}/mapping", json={"mapping": mapping})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "PARSED"
    assert done.json()["entries"] == 2

    bad = ctx.post("/timesheets", files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    assert bad.status_code == 415


def test_timesheet_too_large(ctx: Ctx) -> None:
    big = b"Employee,Day,Hrs,Project\n" + b"Sara,2026-11-05,1,MS-01\n" * 100_000
    r = ctx.post("/timesheets", files={"file": ("big.csv", big, "text/csv")})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


# --- payroll runs -------------------------------------------------------------------------


@respx.mock
def test_run_flow_exclude_then_send_202(ctx: Ctx) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    run = _create_run(ctx)
    assert run["status"] == "AWAITING_APPROVAL"
    assert run["total"] == "720.00"
    assert run["cash_in"] == "850.00"
    assert run["cash_used_pct"] == "84.71"
    assert run["run_flags"] == ["PR-06"]
    omar = _line(run, "Omar")
    assert omar["decision"] == "ASK" and omar["flags"] == ["PR-01"]

    same = _create_run(ctx)
    assert same["run_id"] == run["run_id"]

    listed = ctx.get("/payroll/runs").json()
    assert [r["run_id"] for r in listed] == [run["run_id"]]
    assert ctx.get(f"/payroll/runs/{run['run_id']}").json()["run_id"] == run["run_id"]

    patched = ctx.patch(
        f"/payroll/runs/{run['run_id']}/lines/{omar['line_id']}",
        json={"owner_action": "EXCLUDED", "note": "Confirm new email first"},
    )
    assert patched.status_code == 200, patched.text
    after = patched.json()
    assert after["status"] == "CHECKED"
    assert after["total"] == "420.00"
    assert after["cash_used_pct"] == "49.41"
    assert after["run_decision"] == "ALLOW"
    assert after["run_flags"] == []
    assert _line(after, "Omar")["owner_action"] == "EXCLUDED"

    sent = ctx.post(
        f"/payroll/runs/{run['run_id']}/send",
        json={"confirm": True, "approve_run_flags": [], "idempotency_key": "send_1"},
    )
    assert sent.status_code == 202, sent.text
    assert sent.json() == {
        "run_id": run["run_id"],
        "status": "SENT",
        "sender_batch_id": f"fd-ms01-{run['run_id']}",
        "paypal_batch_id": "5UXD2E8A7EBQJ",
        "batch_status": "PENDING",
        "total": "420.00",
        "lines_sent": 2,
        "lines_excluded": 1,
    }
    assert route.call_count == 1
    assert ctx.polled[-1] == run["run_id"]

    replay = ctx.post(
        f"/payroll/runs/{run['run_id']}/send",
        json={"confirm": True, "approve_run_flags": [], "idempotency_key": "send_1"},
    )
    assert replay.status_code == 202
    assert replay.json()["paypal_batch_id"] == "5UXD2E8A7EBQJ"
    assert route.call_count == 1

    other = ctx.post(
        f"/payroll/runs/{run['run_id']}/send",
        json={"confirm": True, "approve_run_flags": [], "idempotency_key": "send_2"},
    )
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "RUN_NOT_SENDABLE"


@respx.mock
def test_send_with_open_ask_409_body(ctx: Ctx) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    run = _create_run(ctx)
    r = ctx.post(
        f"/payroll/runs/{run['run_id']}/send",
        json={"confirm": True, "approve_run_flags": ["PR-06"], "idempotency_key": "k1"},
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "LEASH_ASK_UNRESOLVED"
    assert err["details"]["open_asks"] == [
        {"line_id": _line(run, "Omar")["line_id"], "flags": ["PR-01"]}
    ]
    assert err["request_id"]
    assert route.call_count == 0


def test_send_requires_literal_confirm(ctx: Ctx) -> None:
    run = _create_run(ctx)
    r = ctx.post(
        f"/payroll/runs/{run['run_id']}/send",
        json={"confirm": False, "approve_run_flags": [], "idempotency_key": "k"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_monthly_mode_and_empty_milestone(ctx: Ctx) -> None:
    monthly = ctx.post("/payroll/runs", json={"mode": "MONTHLY", "period": "2026-11"})
    assert monthly.status_code == 501
    assert monthly.json()["error"]["code"] == "MONTHLY_NOT_AVAILABLE"

    with ctx.sf() as db:
        db.add(
            dm.Milestone(
                id="ms_02",
                contract_id="cnt_01",
                code="MS-02",
                title="Two",
                expected_amount=D("500.00"),
                due_date=date(2026, 12, 1),
                source_clause_quote="q",
            )
        )
        db.commit()
    empty = ctx.post("/payroll/runs", json={"mode": "MILESTONE", "milestone_id": "ms_02"})
    assert empty.status_code == 404
    assert empty.json()["error"]["code"] == "NOT_FOUND"
    assert empty.json()["error"]["message"] == "No timesheet hours for this milestone."


# --- margin -------------------------------------------------------------------------------


def test_margin_endpoint_shapes(ctx: Ctx) -> None:
    r = ctx.get("/ledger/margin")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"rows", "summary"}
    row = body["rows"][0]
    assert row["milestone_id"] == "ms_01" and row["milestone_code"] == "MS-01"
    assert row["cash_in"] == "850.00"
    assert row["paid_to_team"] == "0.00"
    assert row["margin"] == "850.00"
    assert row["margin_pct"] == "100.00"
    assert row["payroll_status"] == "NONE"
    assert body["summary"] == {"cash_in": "850.00", "paid_to_team": "0.00", "margin": "850.00"}


# --- decisions ----------------------------------------------------------------------------


def _add_decisions(c: Ctx, count: int) -> None:
    with c.sf() as db:
        policy = db.scalars(select(lm.LeashPolicyRow)).first()
        assert policy is not None
        for i in range(count):
            db.add(
                lm.LeashDecisionRow(
                    id=f"lsd_{i:04d}",
                    policy_version=policy.version,
                    action_type="SEND_REMEDIATION_INVOICE" if i % 2 == 0 else "PAY_TEAM_MEMBER",
                    subject_type="discrepancy" if i % 2 == 0 else "payroll_line",
                    subject_id=f"sub_{i}",
                    decision="ALLOW",
                    rule_ids_json=["G-01"],
                    facts_json={},
                    amount=D("200.00") if i % 2 == 0 else None,
                    reason="ok",
                    actor="leash" if i % 3 else "owner",
                    created_at=NOW - timedelta(minutes=count - i),
                )
            )
        db.commit()


def test_decisions_pagination(ctx: Ctx) -> None:
    _add_decisions(ctx, 5)
    first = ctx.get("/decisions", params={"limit": 2}).json()
    assert [i["decision_id"] for i in first["items"]] == ["lsd_0004", "lsd_0003"]
    assert first["next_cursor"] is not None
    second = ctx.get("/decisions", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [i["decision_id"] for i in second["items"]] == ["lsd_0002", "lsd_0001"]
    third = ctx.get("/decisions", params={"limit": 2, "cursor": second["next_cursor"]}).json()
    assert [i["decision_id"] for i in third["items"]] == ["lsd_0000"]
    assert third["next_cursor"] is None

    item = first["items"][1]
    assert item["policy_version"] == 1
    assert item["subject_type"] == "payroll_line"
    assert item["amount"] is None
    assert item["actor"] == "owner"
    assert first["items"][0]["actor"] == "leash"
    assert item["rule_ids"] == ["G-01"]
    zero = ctx.get("/decisions", params={"subject_type": "discrepancy"}).json()
    assert [i["decision_id"] for i in zero["items"]] == ["lsd_0004", "lsd_0002", "lsd_0000"]
    assert zero["items"][0]["amount"] == "200.00"
    assert zero["items"][2]["actor"] == "owner"


def test_decisions_bad_params(ctx: Ctx) -> None:
    assert ctx.get("/decisions", params={"limit": 0}).status_code == 422
    assert ctx.get("/decisions", params={"limit": 201}).status_code == 422
    assert ctx.get("/decisions", params={"subject_type": "nope"}).status_code == 422
    bad = ctx.get("/decisions", params={"cursor": "garbage"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


# --- policy -------------------------------------------------------------------------------


def test_policy_plain_form_then_activate(ctx: Ctx) -> None:
    active = ctx.get("/policy").json()
    assert active["version"] == 1
    assert active["activated_at"]

    draft_policy = {
        "version": 99,
        "payroll_margin_limit_pct": "70",
        "email_change_window_days": 5,
        "rules": [
            {
                "id": "G-01",
                "applies_to": "SEND_REMEDIATION_INVOICE",
                "when": {"amount_lte": "100.00", "clause_quote_present": True},
                "outcome": "ALLOW",
                "source_text": "small ones go out",
            },
            {
                "id": "G-02",
                "applies_to": "SEND_REMEDIATION_INVOICE",
                "when": {"amount_gt": "100.00"},
                "outcome": "ASK",
                "source_text": "bigger ones wait",
            },
        ],
    }
    r = ctx.post("/policy", json=draft_policy)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["draft_version"] == 2 and body["status"] == "DRAFT"
    assert body["policy"]["version"] == 2
    assert "G-05" in [rule["id"] for rule in body["policy"]["rules"]]
    assert body["test_cases"] == []
    assert body["cards"] and all(isinstance(c, str) and c for c in body["cards"])
    assert any("100.00" in c for c in body["cards"])

    # Nothing changes until activation.
    assert ctx.get("/policy").json()["version"] == 1
    provider = SqlPolicyProvider(ctx.sf)
    import asyncio

    assert asyncio.run(provider.active_policy()).version == 1

    act = ctx.post("/policy/activate", json={"version": 2})
    assert act.status_code == 200, act.text
    assert act.json()["version"] == 2 and act.json()["active"] is True
    assert act.json()["activated_at"]
    now_active = ctx.get("/policy").json()
    assert now_active["version"] == 2
    assert now_active["payroll_margin_limit_pct"] == "70"
    assert asyncio.run(provider.active_policy()).version == 2
    with ctx.sf() as db:
        rows = db.scalars(select(lm.LeashPolicyRow).where(lm.LeashPolicyRow.active.is_(True)))
        assert [r.version for r in rows] == [2]

    third = ctx.post("/policy", json=draft_policy)
    assert third.json()["draft_version"] == 3

    missing = ctx.post("/policy/activate", json={"version": 42})
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "POLICY_DRAFT_NOT_FOUND"


def test_compile_unavailable_501(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(leash_api, "_load_compiler", lambda: None)
    r = ctx.post("/policy/compile", json={"text": "Send small invoices automatically."})
    assert r.status_code == 501
    err = r.json()["error"]
    assert err["code"] == "COMPILER_UNAVAILABLE"
    assert err["message"] == "Use the plain policy form."


def test_compile_uses_compiler_when_present(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.leash.policy import default_policy

    async def fake(text: str) -> Any:
        assert text.startswith("Send")
        return default_policy().model_copy(update={"version": 7})

    monkeypatch.setattr(leash_api, "_load_compiler", lambda: fake)
    r = ctx.post("/policy/compile", json={"text": "Send small invoices automatically."})
    assert r.status_code == 200, r.text
    assert r.json()["draft_version"] == 2
    assert r.json()["policy"]["version"] == 2
    with ctx.sf() as db:
        row = db.scalars(select(lm.LeashPolicyRow).where(lm.LeashPolicyRow.version == 2)).one()
        assert row.active is False
        assert row.source_text.startswith("Send")


# --- unknown ids --------------------------------------------------------------------------


def test_unknown_ids_404(ctx: Ctx) -> None:
    run = _create_run(ctx)
    line_id = _line(run, "Sara")["line_id"]
    cases = [
        ctx.get("/payroll/runs/prn_missing"),
        ctx.patch(f"/payroll/runs/prn_missing/lines/{line_id}", json={"owner_action": "EXCLUDED"}),
        ctx.patch(
            f"/payroll/runs/{run['run_id']}/lines/prl_missing", json={"owner_action": "EXCLUDED"}
        ),
        ctx.patch("/team/tm_missing", json={"hourly_rate": "20.00"}),
        ctx.post("/timesheets/tsu_missing/mapping", json={"mapping": {"A": "hours"}}),
        ctx.post("/payroll/runs", json={"mode": "MILESTONE", "milestone_id": "ms_missing"}),
        ctx.post(
            "/payroll/runs/prn_missing/send",
            json={"confirm": True, "approve_run_flags": [], "idempotency_key": "k"},
        ),
    ]
    for r in cases:
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == "NOT_FOUND"


# --- fix round 1 ----------------------------------------------------------------------------


def _upload_unmappable(c: Ctx) -> str:
    r = c.post("/timesheets", files={"file": ("j.csv", b"just one header\nx\ny\n", "text/csv")})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "MAPPING_REQUIRED"
    return str(r.json()["upload_id"])


def test_unmappable_upload_blocks_until_discarded(ctx: Ctx) -> None:
    upload_id = _upload_unmappable(ctx)
    body = {"mode": "MILESTONE", "milestone_id": "ms_01"}
    assert ctx.post("/payroll/runs", json=body).json()["error"]["code"] == "MAPPING_REQUIRED"
    assert ctx.post(f"/timesheets/{upload_id}/discard", auth=False).status_code == 401
    done = ctx.post(f"/timesheets/{upload_id}/discard")
    assert done.status_code == 200, done.text
    assert done.json() == {"upload_id": upload_id, "status": "REJECTED"}
    again = ctx.post(f"/timesheets/{upload_id}/discard")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "NOT_DISCARDABLE"
    assert ctx.post("/payroll/runs", json=body).status_code == 200


def test_discard_parsed_or_unknown_upload(ctx: Ctx) -> None:
    with ctx.sf() as db:
        parsed = db.scalars(select(lm.TimesheetUploadRow)).first()
        assert parsed is not None
        parsed_id = parsed.id
    r = ctx.post(f"/timesheets/{parsed_id}/discard")
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_DISCARDABLE"
    missing = ctx.post("/timesheets/tsu_missing/discard")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


def test_existing_open_run_returned_despite_pending_upload(ctx: Ctx) -> None:
    run = _create_run(ctx)
    _upload_unmappable(ctx)
    r = ctx.post("/payroll/runs", json={"mode": "MILESTONE", "milestone_id": "ms_01"})
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == run["run_id"]


def test_zero_row_upload_does_not_block(ctx: Ctx) -> None:
    r = ctx.post("/timesheets", files={"file": ("h.csv", b"just one header\n", "text/csv")})
    assert r.status_code == 201 and r.json()["status"] == "MAPPING_REQUIRED"
    assert r.json()["row_count"] == 0
    run = ctx.post("/payroll/runs", json={"mode": "MILESTONE", "milestone_id": "ms_01"})
    assert run.status_code == 200, run.text


def _policy_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": 1,
        "payroll_margin_limit_pct": "80",
        "email_change_window_days": 7,
        "rules": [
            {
                "id": "G-01",
                "applies_to": "SEND_REMEDIATION_INVOICE",
                "when": {"amount_lte": "100.00"},
                "outcome": "ALLOW",
                "source_text": "small",
            }
        ],
    }
    body.update(over)
    return body


@pytest.mark.parametrize("reserved", ["G-05", "DEFAULT", "PAYROLL-CHECKS"])
def test_reserved_rule_ids_rejected(ctx: Ctx, reserved: str) -> None:
    fake = {
        "id": reserved,
        "applies_to": "SEND_REMEDIATION_INVOICE",
        "when": {"amount_lte": "1000.00"},
        "outcome": "ALLOW",
        "source_text": "sneaky",
    }
    body = _policy_body()
    body["rules"].append(fake)
    r = ctx.post("/policy", json=body)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["fields"] == [{"field": "rules[1].id", "msg": "reserved rule id"}]
    with ctx.sf() as db:
        assert len(db.scalars(select(lm.LeashPolicyRow)).all()) <= 1  # nothing was saved


def test_valid_policy_stores_real_g05_and_card(ctx: Ctx) -> None:
    r = ctx.post("/policy", json=_policy_body())
    assert r.status_code == 200, r.text
    body = r.json()
    g05 = [rule for rule in body["policy"]["rules"] if rule["id"] == "G-05"]
    assert len(g05) == 1
    assert g05[0]["outcome"] == "DENY" and g05[0]["source_text"] == "(built in)"
    assert any("(G-05)" in c and "do not add up" in c for c in body["cards"])
    with ctx.sf() as db:
        row = db.scalars(select(lm.LeashPolicyRow).where(lm.LeashPolicyRow.version == 2)).one()
        stored = [x for x in row.compiled_json["rules"] if x["id"] == "G-05"]
        assert len(stored) == 1 and stored[0]["outcome"] == "DENY"


@pytest.mark.parametrize("days", [1_000_000_000, 366])
def test_email_window_bounded(ctx: Ctx, days: int) -> None:
    r = ctx.post("/policy", json=_policy_body(email_change_window_days=days))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    ok = ctx.post("/policy", json=_policy_body(email_change_window_days=365))
    assert ok.status_code == 200


def test_failed_resume_is_retried(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def flaky() -> int:
        calls.append(1)
        if len(calls) == 1:
            msg = "boom"
            raise RuntimeError(msg)
        return 0

    monkeypatch.setattr(payouts_sync, "resume_pending", flaky)
    assert ctx.get("/team").status_code == 200
    assert ctx.get("/team").status_code == 200
    assert ctx.get("/team").status_code == 200
    assert len(calls) == 2


# --- final-review fixes ---------------------------------------------------------------------


def _activate_rule(ctx: Ctx, rule: dict[str, Any]) -> None:
    policy = ctx.get("/policy").json()
    body = {
        "version": policy["version"],
        "payroll_margin_limit_pct": policy["payroll_margin_limit_pct"],
        "email_change_window_days": policy["email_change_window_days"],
        "rules": [rule],
    }
    r = ctx.post("/policy", json=body)
    assert r.status_code == 200, r.text
    version = r.json()["draft_version"]
    assert ctx.post("/policy/activate", json={"version": version}).status_code == 200


def _send(ctx: Ctx, run_id: str, flags: list[str], key: str = "k") -> httpx.Response:
    return ctx.post(
        f"/payroll/runs/{run_id}/send",
        json={"confirm": True, "approve_run_flags": flags, "idempotency_key": key},
    )


@respx.mock
def test_run_level_policy_ask_blocks_send_until_approved(ctx: Ctx) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    _activate_rule(
        ctx,
        {
            "id": "R-RUN",
            "applies_to": "SEND_PAYROLL_BATCH",
            "when": {"amount_gt": "300.00"},
            "outcome": "ASK",
            "source_text": "ask me for big payroll",
        },
    )
    run = _create_run(ctx)
    omar = _line(run, "Omar")
    after = ctx.patch(
        f"/payroll/runs/{run['run_id']}/lines/{omar['line_id']}", json={"owner_action": "EXCLUDED"}
    ).json()
    assert after["run_decision"] == "ASK"
    assert after["run_flags"] == ["R-RUN"]
    assert after["status"] == "AWAITING_APPROVAL"

    blocked = _send(ctx, run["run_id"], [])
    assert blocked.status_code == 409, blocked.text
    error = blocked.json()["error"]
    assert error["code"] == "LEASH_ASK_UNRESOLVED"
    assert error["details"]["open_run_flags"] == ["R-RUN"]
    assert route.call_count == 0

    ok = _send(ctx, run["run_id"], ["R-RUN"])
    assert ok.status_code == 202, ok.text
    assert route.call_count == 1


@respx.mock
def test_run_deny_policy_stays_denied(ctx: Ctx) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    _activate_rule(
        ctx,
        {
            "id": "R-NO",
            "applies_to": "SEND_PAYROLL_BATCH",
            "when": {"amount_gt": "300.00"},
            "outcome": "DENY",
            "source_text": "never pay that much",
        },
    )
    run = _create_run(ctx)
    r = _send(ctx, run["run_id"], ["PR-06", "R-NO"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "LEASH_DENIED"
    assert route.call_count == 0


@respx.mock
def test_policy_only_line_ask_shows_rule_id(ctx: Ctx) -> None:
    respx.post(PAYOUTS_URL).mock(return_value=_created())
    _activate_rule(
        ctx,
        {
            "id": "R-LINE",
            "applies_to": "PAY_TEAM_MEMBER",
            "when": {"amount_gt": "210.00"},
            "outcome": "ASK",
            "source_text": "ask me for pay over 210",
        },
    )
    run = _create_run(ctx)
    sara = _line(run, "Sara")  # 220.00, no payroll flag
    assert sara["decision"] == "ASK"
    assert sara["flags"] == ["R-LINE"]
    blocked = _send(ctx, run["run_id"], ["PR-06"])
    assert blocked.status_code == 409
    asks = blocked.json()["error"]["details"]["open_asks"]
    assert {"line_id": sara["line_id"], "flags": ["R-LINE"]} in asks


@respx.mock
def test_email_changed_during_the_gate_is_never_paid(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.post(PAYOUTS_URL).mock(return_value=_created())
    run = _create_run(ctx)
    run_id = run["run_id"]
    omar = _line(run, "Omar")
    r = ctx.patch(
        f"/payroll/runs/{run_id}/lines/{omar['line_id']}", json={"owner_action": "EXCLUDED"}
    )
    assert r.status_code == 200
    sara_id = next(m["member_id"] for m in ctx.get("/team").json() if m["name"] == "Sara")

    real_decide = runs.decide
    state = {"n": 0}

    async def slow_decide(action: GuardAction, facts: dict[str, Any], subject_id: str) -> Any:
        # While the send gate's second check is in flight, Sara's payout email is changed.
        if action == GuardAction.SEND_PAYROLL_BATCH:
            state["n"] += 1
            if state["n"] == 2:

                def _swap() -> None:
                    with ctx.sf() as db:
                        runs.update_member(
                            db, sara_id, TeamMemberUpdate(payout_email="attacker@example.com")
                        )

                await asyncio.to_thread(_swap)
        return await real_decide(action, facts, subject_id)

    monkeypatch.setattr(runs, "decide", slow_decide)
    s = _send(ctx, run_id, [])
    assert s.status_code == 409, s.text
    assert s.json()["error"]["code"] == "RUN_NOT_SENDABLE"
    assert route.call_count == 0
    with ctx.sf() as db:
        row = db.get(lm.PayrollRunRow, run_id)
        assert row is not None and row.sender_batch_id is None


def test_new_run_is_checked_once(ctx: Ctx) -> None:
    with ctx.sf() as db:
        before = db.query(lm.LeashDecisionRow).count()
    _create_run(ctx)
    ctx.client.portal.call(runs.wait_for_background)  # let any background check finish
    with ctx.sf() as db:
        rows = db.query(lm.LeashDecisionRow).count() - before
    assert rows == 4  # three lines and the run, not eight

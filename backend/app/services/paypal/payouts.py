"""PayPal Payouts batch client for payroll runs (A.2.3, A.4).

The rule is that nobody gets paid twice. ``send_payroll_batch`` therefore runs under the
same per-run lock the payroll module uses, persists ``sender_batch_id`` and
``idempotency_key`` before the POST, and never POSTs a second batch for a run that
already holds a ``sender_batch_id``. When the outcome is unknown (timeouts, 5xx after
the retries, the duplicate-batch rejection) the run goes to ``SENT`` with
``batch_status = "UNKNOWN"`` so polling, webhooks or the owner can reconcile it; only an
explicit PayPal ``DENIED`` makes a run FAILED. Emails are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select, update

from ...api.errors import ApiError
from ...db.database import utcnow
from ...db.models import Contract, Milestone
from ...leash.models import PayrollLineRow, PayrollRunRow
from ...leash.payroll import runs
from ...schemas.contracts import PayrollLineOut
from ._client import PayPalAPIError, PayPalUnavailable, paypal_request
from .auth import PayPalAuthError

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_PAYOUTS_PATH = "/v1/payments/payouts"
_DEFINITE_REJECTIONS = (400, 422)
TERMINAL_BATCH_STATUSES = frozenset({"SUCCESS", "DENIED", "CANCELED"})


def _money(value: Decimal) -> str:
    return format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


def sender_batch_id_for(milestone_code: str, run_id: str) -> str:
    """``fd-<code lower, dashes removed>-<run id>`` (PIT-08: unique per run)."""
    return f"fd-{milestone_code.lower().replace('-', '')}-{run_id}"


def request_id_for(run_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{run_id}:payout"))


def build_batch(
    run: PayrollRunRow,
    lines: list[PayrollLineOut],
    milestone: Milestone,
    currency: str = "USD",
) -> dict[str, Any]:
    """The A.2.3 request body. Amounts are 2-dp strings; receivers are current emails."""
    return {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id_for(milestone.code, run.id),
            "email_subject": f"You have a payment for milestone {milestone.code}",
            "email_message": f"Thank you for your work on {milestone.title}.",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": ln.payout_email,
                "amount": {"value": _money(ln.gross), "currency": currency},
                "note": f"{milestone.code} · {_money(ln.hours)} h x ${_money(ln.rate)}",
                "sender_item_id": ln.line_id,
            }
            for ln in lines
        ],
    }


async def get_batch(paypal_batch_id: str) -> dict[str, Any]:
    """GET a batch with its items. GET is idempotent, so a random request id is fine."""
    return await paypal_request(
        "GET", f"{_PAYOUTS_PATH}/{paypal_batch_id}", request_id=str(uuid.uuid4())
    )


def _not_sendable(message: str) -> ApiError:
    return ApiError(409, "RUN_NOT_SENDABLE", message)


_ALREADY_SENT = "This payroll run was already sent to PayPal. Reconcile it before trying again."


@dataclass
class _Reserved:
    body: dict[str, Any]
    request_id: str


def _existing(run_id: str, idempotency_key: str) -> str | None:
    """The stored batch id for a repeat call, None for a fresh run, else a 409."""
    with runs.session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Payroll run not found.")
        if run.paypal_batch_id is not None:
            if run.idempotency_key == idempotency_key:
                return run.paypal_batch_id
            raise _not_sendable(_ALREADY_SENT)
        if run.sender_batch_id is not None or run.status in runs.FINISHED:
            raise _not_sendable(_ALREADY_SENT)
        return None


def _reserve(run_id: str, lines: list[PayrollLineOut], idempotency_key: str) -> _Reserved:
    """Persist the reservation (batch id, key, total, PENDING lines); return the body to POST."""
    with runs.session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Payroll run not found.")
        if run.sender_batch_id is not None or run.status in runs.FINISHED:
            raise _not_sendable(_ALREADY_SENT)
        if not lines:
            raise _not_sendable("This payroll run has no lines to pay.")
        milestone = db.get(Milestone, run.milestone_id) if run.milestone_id else None
        if milestone is None:
            # Monthly runs have no milestone: label the batch with the period.
            label = run.period or run.id
            milestone = Milestone(code=label, title=label)
        contract = db.get(Contract, milestone.contract_id) if milestone.contract_id else None
        currency = contract.currency if contract is not None else "USD"
        body = build_batch(run, lines, milestone, currency)
        claimed = db.execute(
            update(PayrollRunRow)
            .where(
                PayrollRunRow.id == run_id,
                PayrollRunRow.status.not_in(tuple(runs.FINISHED)),
                PayrollRunRow.sender_batch_id.is_(None),
            )
            .values(
                sender_batch_id=body["sender_batch_header"]["sender_batch_id"],
                idempotency_key=idempotency_key,
                total=sum((ln.gross for ln in lines), Decimal("0.00")),
            )
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            raise _not_sendable(_ALREADY_SENT)
        ids = {ln.line_id for ln in lines}
        for row in db.scalars(select(PayrollLineRow).where(PayrollLineRow.run_id == run_id)):
            if row.id in ids:
                row.payout_status = "PENDING"  # part of the batch from now on
        db.commit()
        return _Reserved(body, request_id_for(run_id))


def _mark_sent(
    run_id: str,
    line_ids: list[str],
    paypal_batch_id: str | None,
    batch_status: str,
) -> None:
    with runs.session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is None:
            return
        if paypal_batch_id is not None:
            run.paypal_batch_id = paypal_batch_id
        # A webhook may have landed since the 201: never regress a terminal batch status.
        if run.batch_status not in TERMINAL_BATCH_STATUSES:
            run.batch_status = batch_status[:16]
        run.sent_at = run.sent_at or utcnow()
        run.status = "SENT"
        for row in db.scalars(select(PayrollLineRow).where(PayrollLineRow.run_id == run_id)):
            if row.id in line_ids and row.payout_status is None:
                row.payout_status = "PENDING"
        db.commit()
    runs.recompute_run_status(run_id)


def _release(run_id: str) -> None:
    """PayPal definitely did not create a batch: free the reservation (run stays unsent)."""
    with runs.session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is not None and run.paypal_batch_id is None and run.status not in runs.FINISHED:
            run.sender_batch_id = None
            run.idempotency_key = None
            for row in db.scalars(select(PayrollLineRow).where(PayrollLineRow.run_id == run_id)):
                if row.payout_status == "PENDING":
                    row.payout_status = None
            db.commit()


def _is_duplicate(exc: PayPalAPIError) -> bool:
    text = exc.message.lower()
    return "sender_batch_id" in text or "already exists" in text


def _unknown(run_id: str, line_ids: list[str], reason: str) -> None:
    _mark_sent(run_id, line_ids, None, "UNKNOWN")
    logger.error("payroll.batch_unknown", extra={"run_id": run_id, "reason": reason})


async def send_payroll_batch(
    run_id: str,
    lines: list[PayrollLineOut],
    idempotency_key: str,
    *,
    approve_run_flags: Sequence[str] = (),
) -> str:
    """POST one Payouts batch for the run and return PayPal's batch id.

    Safe to call twice: the same ``idempotency_key`` returns the stored batch id, any
    other call on a run that already has a ``sender_batch_id`` is a 409. The caller's
    ``lines`` only name what it expects to pay: under the run lock the whole gate is
    re-run and the batch is built from that fresh database result alone (gross and the
    roster's current email). If the two sets of line ids differ the run changed
    meanwhile, so nothing is sent.
    """
    async with runs.run_lock(run_id):
        stored = await asyncio.to_thread(_existing, run_id, idempotency_key)
        if stored is not None:
            return stored
        ready = await runs._lines_ready_locked(run_id, list(approve_run_flags))
        if {ln.line_id for ln in lines} != {ln.line_id for ln in ready}:
            raise _not_sendable("The payroll run changed. Refresh it and try again.")
        lines = ready
        line_ids = [ln.line_id for ln in lines]
        reserved = await asyncio.to_thread(_reserve, run_id, lines, idempotency_key)
        try:
            result = await paypal_request(
                "POST", _PAYOUTS_PATH, request_id=reserved.request_id, json_body=reserved.body
            )
        except PayPalAPIError as exc:
            if _is_duplicate(exc):
                _unknown(run_id, line_ids, "duplicate sender_batch_id")
                raise _not_sendable(_ALREADY_SENT) from exc
            if exc.status_code in _DEFINITE_REJECTIONS:
                await asyncio.to_thread(_release, run_id)
                raise
            _unknown(run_id, line_ids, f"paypal {exc.status_code}")
            raise ApiError(
                503, "PAYPAL_UNAVAILABLE", "PayPal did not confirm the payout. Reconcile the run."
            ) from exc
        except PayPalAuthError:
            await asyncio.to_thread(_release, run_id)  # never reached the payouts API
            raise
        except PayPalUnavailable as exc:
            _unknown(run_id, line_ids, "unavailable")
            raise ApiError(
                503, "PAYPAL_UNAVAILABLE", "PayPal did not confirm the payout. Reconcile the run."
            ) from exc
        except BaseException:
            _unknown(run_id, line_ids, "interrupted")
            raise

        header = result.get("batch_header") if isinstance(result, dict) else None
        batch_id = header.get("payout_batch_id") if isinstance(header, dict) else None
        if not isinstance(batch_id, str) or not batch_id:
            _unknown(run_id, line_ids, "no batch id in response")
            raise ApiError(
                503, "PAYPAL_UNAVAILABLE", "PayPal did not confirm the payout. Reconcile the run."
            )
        batch_status = str(header.get("batch_status") or "PENDING") if header else "PENDING"
        await asyncio.to_thread(_mark_sent, run_id, line_ids, batch_id, batch_status)
        return batch_id

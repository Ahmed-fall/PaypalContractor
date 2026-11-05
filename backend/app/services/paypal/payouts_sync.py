"""Payout status sync: polling and webhooks feed ``apply_item_status`` (A.5).

PayPal's item statuses collapse into our four (``ITEM_STATUS_MAP``). A terminal status
(SUCCESS, FAILED, UNCLAIMED) never regresses, except that UNCLAIMED may still become
SUCCESS (claimed) or FAILED (returned/refunded). Only a batch ``DENIED`` or an item
status moves a run to FAILED. Database work happens in sync sessions through
``runs.session_factory`` (tests point it at a temp DB). Emails are never logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError

from ...config import get_settings
from ...db.database import utcnow
from ...leash.models import PayrollLineRow, PayrollRunRow, TeamMemberRow
from ...leash.payroll import runs
from ._client import PayPalAPIError, PayPalUnavailable
from .auth import PayPalAuthError
from .payouts import TERMINAL_BATCH_STATUSES, get_batch

logger = logging.getLogger(__name__)

ITEM_STATUS_MAP: dict[str, str] = {
    "SUCCESS": "SUCCESS",
    "UNCLAIMED": "UNCLAIMED",
    "PENDING": "PENDING",
    "ONHOLD": "PENDING",
    "NEW": "PENDING",
    "FAILED": "FAILED",
    "RETURNED": "FAILED",
    "REFUNDED": "FAILED",
    "REVERSED": "FAILED",
    "BLOCKED": "FAILED",
    "DENIED": "FAILED",
    "CANCELED": "FAILED",
}

_TERMINAL = frozenset({"SUCCESS", "FAILED", "UNCLAIMED"})
# Event suffix -> PayPal item status, used only when the payload has no transaction_status.
_EVENT_STATUS = {
    "SUCCEEDED": "SUCCESS",
    "FAILED": "FAILED",
    "BLOCKED": "BLOCKED",
    "CANCELED": "CANCELED",
    "DENIED": "DENIED",
    "HELD": "ONHOLD",
    "REFUNDED": "REFUNDED",
    "RETURNED": "RETURNED",
    "UNCLAIMED": "UNCLAIMED",
}

_tasks: set[asyncio.Task[None]] = set()
_pollers: dict[str, asyncio.Task[None]] = {}


# Statuses a line may leave for each target; None (never sent) is always allowed.
_SOURCES: dict[str, tuple[str, ...]] = {
    "PENDING": ("PENDING",),
    "UNCLAIMED": ("PENDING",),
    "SUCCESS": ("PENDING", "UNCLAIMED"),
    "FAILED": ("PENDING", "UNCLAIMED"),
}


def _allowed(current: str | None, new: str) -> bool:
    """May a line move from ``current`` to ``new``? Terminal statuses never regress."""
    if current is None or current == "PENDING":
        return True
    if current == "UNCLAIMED":
        return new in ("SUCCESS", "FAILED")
    return False


def apply_item_status(
    sender_item_id: str,
    transaction_status: str,
    payout_item_id: str | None = None,
    payout_batch_id: str | None = None,
) -> None:
    """Apply one PayPal item status to its payroll line, then roll the run up."""
    mapped = ITEM_STATUS_MAP.get(transaction_status.upper())
    if mapped is None:
        logger.warning("payouts.unknown_status", extra={"status": transaction_status[:32]})
        return
    with runs.session_factory() as db:
        line = db.get(PayrollLineRow, sender_item_id)
        if line is None:
            logger.warning("payouts.unknown_line", extra={"sender_item_id": sender_item_id[:40]})
            return
        run = db.get(PayrollRunRow, line.run_id)
        # The reservation (sender_batch_id) is persisted before the POST, so a webhook that
        # beats our own bookkeeping still applies; only lines that were in the batch count.
        in_batch = run is not None and (
            run.status in runs.FINISHED or run.sender_batch_id is not None
        )
        if run is None or not in_batch or line.payout_status is None:
            logger.warning("payouts.line_not_sent", extra={"sender_item_id": sender_item_id[:40]})
            return
        run_id = run.id
        if (
            payout_batch_id
            and run.paypal_batch_id is None
            and run.batch_status == "UNKNOWN"
            and db.scalars(
                select(PayrollRunRow.id).where(PayrollRunRow.paypal_batch_id == payout_batch_id)
            ).first()
            is None
        ):
            run.paypal_batch_id = payout_batch_id[:64]  # reconciles an UNKNOWN run
            db.commit()
        if _allowed(line.payout_status, mapped):
            # Conditional write: a concurrent terminal status written since the read wins.
            values: dict[str, Any] = {
                "paypal_transaction_status": transaction_status.upper()[:16],
                "payout_status": mapped,
            }
            if payout_item_id:
                values["payout_item_id"] = func.coalesce(
                    PayrollLineRow.payout_item_id, payout_item_id[:64]
                )
            written = db.execute(
                update(PayrollLineRow)
                .where(
                    PayrollLineRow.id == sender_item_id,
                    or_(
                        PayrollLineRow.payout_status.is_(None),
                        PayrollLineRow.payout_status.in_(_SOURCES[mapped]),
                    ),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if written.rowcount == 1 and mapped == "SUCCESS":  # type: ignore[attr-defined]
                member = db.get(TeamMemberRow, line.team_member_id)
                if member is not None and member.first_paid_at is None:
                    member.first_paid_at = utcnow()
            db.commit()
    runs.recompute_run_status(run_id)


# --- batch-level updates ------------------------------------------------------------------


def _set_batch_status(
    *, paypal_batch_id: str | None, sender_batch_id: str | None, batch_status: str
) -> str | None:
    """Update the matching sent run's batch_status; returns its id (None when no match)."""
    with runs.session_factory() as db:
        run = None
        if paypal_batch_id:
            run = db.scalars(
                select(PayrollRunRow).where(PayrollRunRow.paypal_batch_id == paypal_batch_id)
            ).first()
        if run is None and sender_batch_id:
            run = db.scalars(
                select(PayrollRunRow).where(
                    PayrollRunRow.sender_batch_id == sender_batch_id,
                    PayrollRunRow.paypal_batch_id.is_(None),
                )
            ).first()
            if run is not None and paypal_batch_id:
                run.paypal_batch_id = paypal_batch_id  # reconciles an UNKNOWN run
        if run is None or not (run.status in runs.FINISHED or run.sender_batch_id is not None):
            return None
        run_id = run.id
        if run.batch_status in TERMINAL_BATCH_STATUSES:
            return run_id  # a late or out-of-order batch event never regresses a final status
        run.batch_status = batch_status.upper()[:16]
        db.commit()
    runs.recompute_run_status(run_id)
    return run_id


def _apply_batch_items(items: Any) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        payout_item = item.get("payout_item")
        sender_item_id = (
            payout_item.get("sender_item_id") if isinstance(payout_item, dict) else None
        )
        status = item.get("transaction_status")
        if isinstance(sender_item_id, str) and isinstance(status, str):
            item_id = item.get("payout_item_id")
            apply_item_status(sender_item_id, status, item_id if isinstance(item_id, str) else None)


def _poll_state(run_id: str) -> tuple[str | None, bool]:
    """(paypal_batch_id, everything settled) for a run."""
    with runs.session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is None:
            return None, True
        sent = [
            ln.payout_status
            for ln in db.scalars(select(PayrollLineRow).where(PayrollLineRow.run_id == run_id))
            if ln.payout_status is not None
        ]
        settled = run.batch_status == "DENIED" or (bool(sent) and all(s in _TERMINAL for s in sent))
        return run.paypal_batch_id, settled


async def _poll_once(run_id: str) -> bool:
    """One poll tick; True when the run is settled (or cannot be polled) and polling can stop."""
    batch_id, settled = await asyncio.to_thread(_poll_state, run_id)
    if settled:
        return True
    if batch_id is None:
        logger.warning("payouts.poll_no_batch_id", extra={"run_id": run_id})
        return True
    try:
        batch = await get_batch(batch_id)
    except (PayPalAPIError, PayPalUnavailable, PayPalAuthError):
        logger.warning("payouts.poll_failed", extra={"run_id": run_id}, exc_info=True)
        return False
    await asyncio.to_thread(_apply_batch_items, batch.get("items"))
    header = batch.get("batch_header")
    status = header.get("batch_status") if isinstance(header, dict) else None
    if isinstance(status, str):
        await asyncio.to_thread(
            _set_batch_status, paypal_batch_id=batch_id, sender_batch_id=None, batch_status=status
        )
    _, settled = await asyncio.to_thread(_poll_state, run_id)
    return settled


async def poll_batch(
    run_id: str,
    *,
    interval_s: float | None = None,
    max_s: float | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] | None = None,
) -> None:
    """Poll the run's batch until every item is terminal or ``max_s`` has passed."""
    settings = get_settings()
    interval = float(interval_s if interval_s is not None else settings.payouts_poll_seconds)
    limit = float(max_s if max_s is not None else settings.payouts_poll_max_seconds)
    now = clock or asyncio.get_running_loop().time
    started = now()
    while True:
        try:
            if await _poll_once(run_id):
                return
        except OperationalError:  # e.g. SQLite busy: try again on the next tick
            logger.warning("payouts.poll_db_busy", extra={"run_id": run_id}, exc_info=True)
        if now() - started >= limit:
            return
        await sleep(interval)


def _poller_done(run_id: str, task: asyncio.Task[None]) -> None:
    _tasks.discard(task)
    if _pollers.get(run_id) is task:
        del _pollers[run_id]
    if not task.cancelled() and task.exception() is not None:
        logger.error("payouts.poll_crashed", extra={"run_id": run_id}, exc_info=task.exception())


def start_polling(run_id: str) -> asyncio.Task[None]:
    """Start (once) a background poller for the run; call from inside the event loop."""
    existing = _pollers.get(run_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.get_running_loop().create_task(poll_batch(run_id))
    _tasks.add(task)
    _pollers[run_id] = task
    task.add_done_callback(lambda t: _poller_done(run_id, t))
    return task


def _pending_run_ids() -> list[str]:
    with runs.session_factory() as db:
        return [
            r.id
            for r in db.scalars(
                select(PayrollRunRow).where(
                    PayrollRunRow.status == "SENT", PayrollRunRow.paypal_batch_id.is_not(None)
                )
            )
            if r.batch_status != "DENIED"
        ]


async def resume_pending() -> int:
    """Start polling every SENT run with a batch id; returns how many were started."""
    run_ids = await asyncio.to_thread(_pending_run_ids)
    for run_id in run_ids:
        start_polling(run_id)
    return len(run_ids)


# --- webhooks -------------------------------------------------------------------------------


def _handle_item_event(event_type: str, resource: dict[str, Any]) -> None:
    payout_item = resource.get("payout_item")
    sender_item_id = payout_item.get("sender_item_id") if isinstance(payout_item, dict) else None
    status = resource.get("transaction_status")
    if not isinstance(status, str):
        status = _EVENT_STATUS.get(event_type.rsplit(".", 1)[-1])
    if not isinstance(sender_item_id, str) or not isinstance(status, str):
        logger.warning("payouts.webhook_malformed", extra={"event_type": event_type})
        return
    item_id = resource.get("payout_item_id")
    batch_id = resource.get("payout_batch_id")
    apply_item_status(
        sender_item_id,
        status,
        item_id if isinstance(item_id, str) else None,
        batch_id if isinstance(batch_id, str) else None,
    )


def _handle_batch_event(event_type: str, resource: dict[str, Any]) -> None:
    header = resource.get("batch_header")
    if not isinstance(header, dict):
        logger.warning("payouts.webhook_malformed", extra={"event_type": event_type})
        return
    batch_id = header.get("payout_batch_id")
    status = header.get("batch_status")
    sender_header = header.get("sender_batch_header")
    sender_batch_id = (
        sender_header.get("sender_batch_id") if isinstance(sender_header, dict) else None
    )
    if not isinstance(status, str) or not (
        isinstance(batch_id, str) or isinstance(sender_batch_id, str)
    ):
        logger.warning("payouts.webhook_malformed", extra={"event_type": event_type})
        return
    matched = _set_batch_status(
        paypal_batch_id=batch_id if isinstance(batch_id, str) else None,
        sender_batch_id=sender_batch_id if isinstance(sender_batch_id, str) else None,
        batch_status=status,
    )
    if matched is None:
        logger.info("payouts.webhook_no_run", extra={"event_type": event_type})


async def handle_webhook_event(event: dict[str, Any]) -> None:
    """Entry point for ``leash_hooks.dispatch_payouts_event``.

    Malformed payloads are logged and ignored (a raised error makes PayPal retry); only a
    real database failure propagates.
    """
    event_type = event.get("event_type") if isinstance(event, dict) else None
    resource = event.get("resource") if isinstance(event, dict) else None
    if not isinstance(event_type, str) or not isinstance(resource, dict):
        logger.warning("payouts.webhook_malformed")
        return
    if event_type.startswith("PAYMENT.PAYOUTS-ITEM."):
        await asyncio.to_thread(_handle_item_event, event_type, resource)
    elif event_type.startswith("PAYMENT.PAYOUTSBATCH."):
        await asyncio.to_thread(_handle_batch_event, event_type, resource)


async def wait_for_polling() -> None:
    """Await every running poller (used by tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


__all__ = [
    "ITEM_STATUS_MAP",
    "apply_item_status",
    "handle_webhook_event",
    "poll_batch",
    "resume_pending",
    "start_polling",
    "wait_for_polling",
]

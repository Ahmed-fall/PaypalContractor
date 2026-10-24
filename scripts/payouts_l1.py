"""Payouts L1 proof: send a real 2-item PayPal sandbox Payouts batch and verify it.

Run from the repo root:
``python -m scripts.payouts_l1 --to EMAIL --to EMAIL [--save-fixtures]``.
The token comes only from ``get_auth_manager()``; the base URL from ``get_settings()``
(sandbox-guarded). Full recipient emails are never printed or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx
from ulid import ULID

from backend.app.config import get_settings
from backend.app.services.paypal.auth import PayPalAuthError, get_auth_manager

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "shared" / "fixtures" / "leash" / "paypal"

POLL_INTERVAL_S = 10
POLL_MAX_S = 120
TIMEOUT_S = 15.0

EMAIL_SUBJECT = "Leash L1 sandbox payout test"
NON_TERMINAL_BATCH = {"NEW", "PENDING", "PROCESSING"}
NON_TERMINAL_ITEM = {"PENDING", "ONHOLD"}
RETRY_DELAYS_S = (1, 3)
EVENT_TYPE_ITEM_SUCCEEDED = "PAYMENT.PAYOUTS-ITEM.SUCCEEDED"
MAX_AMOUNT = Decimal("10.00")
STEP_ERRORS = (httpx.HTTPError, PayPalAuthError)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
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


class AuthLike(Protocol):
    async def get_access_token(self) -> str: ...

    def invalidate(self) -> None: ...


class PayoutsClient:
    """Minimal Payouts HTTP client following the A.4 timeout and retry rules."""

    def __init__(
        self,
        base_url: str,
        auth: AuthLike,
        http: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._http = http
        self.sleep = sleep

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        request_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        refreshed = False
        retries = 0
        while True:
            headers = {"Authorization": f"Bearer {await self._auth.get_access_token()}"}
            if request_id is not None:
                headers["PayPal-Request-Id"] = request_id
            try:
                response = await self._http.request(
                    method,
                    f"{self._base_url}{path}",
                    json=json_body,
                    params=params,
                    headers=headers,
                    timeout=TIMEOUT_S,
                )
            except httpx.TimeoutException:
                if retries < len(RETRY_DELAYS_S):
                    await self.sleep(RETRY_DELAYS_S[retries])
                    retries += 1
                    continue
                raise
            logger.info(
                "paypal %s %s -> %s paypal-debug-id=%s",
                method,
                path,
                response.status_code,
                response.headers.get("paypal-debug-id"),
            )
            if response.status_code == 401 and not refreshed:
                self._auth.invalidate()
                refreshed = True
                continue
            if response.status_code >= 500 and retries < len(RETRY_DELAYS_S):
                await self.sleep(RETRY_DELAYS_S[retries])
                retries += 1
                continue
            return response

    async def create_batch(
        self, body: dict[str, Any], request_id: str
    ) -> httpx.Response:
        return await self._send(
            "POST", "/v1/payments/payouts", json_body=body, request_id=request_id
        )

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        response = await self._send("GET", f"/v1/payments/payouts/{batch_id}")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def get_item(self, item_id: str) -> dict[str, Any]:
        response = await self._send("GET", f"/v1/payments/payouts-item/{item_id}")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def latest_webhook_event(
        self, event_type: str, payout_batch_id: str
    ) -> dict[str, Any] | None:
        """Best effort: newest stored event of this type for THIS batch, else ``None``."""
        try:
            response = await self._send(
                "GET",
                "/v1/notifications/webhooks-events",
                params={"event_type": event_type, "page_size": 10},
            )
            if response.status_code != 200:
                return None
            events = response.json().get("events") or []
        except (*STEP_ERRORS, ValueError, AttributeError):
            return None
        for event in events:
            resource = event.get("resource") if isinstance(event, dict) else None
            if (
                isinstance(resource, dict)
                and resource.get("payout_batch_id") == payout_batch_id
            ):
                return event  # type: ignore[no-any-return]
        return None


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class L1Result:
    checks: list[Check] = field(default_factory=list)
    payloads: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(check.passed for check in self.checks)


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _scrub(text: str, recipients: list[str]) -> str:
    """Mask any recipient email appearing in free text from PayPal."""
    for email in recipients:
        text = re.sub(
            re.escape(email.strip()),
            mask_email(email.strip()),
            text,
            flags=re.IGNORECASE,
        )
    return text


def _debug_suffix(response: httpx.Response) -> str:
    debug_id = response.headers.get("paypal-debug-id")
    return f" paypal-debug-id={debug_id}" if debug_id else ""


def _classify_duplicate(
    response: httpx.Response, body: dict[str, Any], recipients: list[str]
) -> Check:
    """Pass only when PayPal rejects the reused sender_batch_id for that reason."""
    name = "duplicate_batch_rejected"
    status = response.status_code
    if status == 201:
        second = (body.get("batch_header") or {}).get("payout_batch_id")
        return Check(
            name,
            False,
            "DOUBLE PAY: PayPal accepted a reused sender_batch_id "
            f"(second payout_batch_id={second})",
        )
    if not 400 <= status < 500 or "batch_header" in body:
        return Check(name, False, f"unexpected HTTP {status}{_debug_suffix(response)}")
    details = [d for d in body.get("details") or [] if isinstance(d, dict)]
    issue = details[0].get("issue") if details else None
    texts = [str(body.get("name", "")), str(body.get("message", ""))]
    for detail in details:
        texts.extend(str(detail.get(k, "")) for k in ("issue", "field", "description"))
    blob = " ".join(texts).lower()
    if "sender_batch_id" in blob or "already" in blob:
        text = f"HTTP {status} {body.get('name')}: {issue}"
        return Check(name, True, _scrub(text, recipients))
    text = f"rejected for another reason: {body.get('name')}: {issue}"
    return Check(name, False, _scrub(text, recipients) + _debug_suffix(response))


async def run_l1(
    client: PayoutsClient,
    recipients: list[str],
    amount: Decimal,
    currency: str,
    run_id: str,
) -> L1Result:
    result = L1Result()
    body = build_batch(run_id, recipients, amount, currency)
    sent_ids = [item["sender_item_id"] for item in body["items"]]
    sender_batch_id = body["sender_batch_header"]["sender_batch_id"]
    masked = ", ".join(mask_email(email) for email in recipients)
    print(f"sender_batch_id: {sender_batch_id}")
    print(f"recipients: {masked}")

    # 1. Create the batch.
    reconcile = (
        f"batch may exist at PayPal: reconcile sender_batch_id {sender_batch_id} "
        "before re-running"
    )
    try:
        created = await client.create_batch(body, payout_request_id(run_id, "payout"))
    except STEP_ERRORS as exc:
        result.checks.append(Check("batch_created", False, type(exc).__name__))
        if isinstance(exc, httpx.HTTPError):
            print(reconcile)
        return result
    created_body = _json_or_empty(created)
    header = created_body.get("batch_header") or {}
    created_ok = created.status_code == 201 and header.get("batch_status") == "PENDING"
    detail = f"HTTP {created.status_code} batch_status={header.get('batch_status')}"
    result.checks.append(
        Check(
            "batch_created",
            created_ok,
            detail if created_ok else detail + _debug_suffix(created),
        )
    )
    if created.status_code >= 500:
        print(reconcile)
    if created.status_code != 201:
        return result
    result.payloads["batch_create_201"] = created_body
    batch_id = header.get("payout_batch_id")
    if not batch_id:
        result.checks.append(
            Check("batch_id_present", False, "no payout_batch_id in 201")
        )
        return result
    print(f"payout_batch_id: {batch_id}")

    # 2. Poll on simulated time until terminal or POLL_MAX_S.
    elapsed = 0
    final: dict[str, Any] | None = None
    try:
        final = await client.get_batch(batch_id)
        while not is_batch_terminal(final) and elapsed < POLL_MAX_S:
            await client.sleep(POLL_INTERVAL_S)
            elapsed += POLL_INTERVAL_S
            final = await client.get_batch(batch_id)
    except STEP_ERRORS as exc:
        result.checks.append(Check("batch_poll", False, type(exc).__name__))
    items: list[dict[str, Any]] = []
    if final is not None:
        result.payloads["batch_final"] = final

        # 3. Batch and item outcomes.
        status = (final.get("batch_header") or {}).get("batch_status")
        result.checks.append(
            Check(
                "batch_success",
                status == "SUCCESS",
                f"batch_status={status} after {elapsed}s",
            )
        )
        items = final.get("items") or []
        echoed = [(i.get("payout_item") or {}).get("sender_item_id") for i in items]
        all_success = bool(items) and all(
            i.get("transaction_status") == "SUCCESS" for i in items
        )
        ids_match = sorted(str(e) for e in echoed) == sorted(sent_ids)
        if all_success and ids_match:
            items_detail = f"{len(items)} items SUCCESS, sender_item_ids echoed"
        else:
            listing = ", ".join(
                f"{mask_email(str((i.get('payout_item') or {}).get('receiver', '?')))}"
                f" -> {i.get('transaction_status')}"
                for i in items
            )
            items_detail = f"{listing or 'no items'}; sender_item_ids_match={ids_match}"
        result.checks.append(
            Check("items_success", all_success and ids_match, items_detail)
        )

    # 4. Resend the identical body: PayPal must reject the reused sender_batch_id.
    try:
        dup = await client.create_batch(
            body, payout_request_id(run_id, "payout-resend")
        )
    except STEP_ERRORS as exc:
        result.checks.append(
            Check("duplicate_batch_rejected", False, type(exc).__name__)
        )
    else:
        dup_body = _json_or_empty(dup)
        result.checks.append(_classify_duplicate(dup, dup_body, recipients))
        key = (
            "duplicate_accepted_201" if dup.status_code == 201 else "duplicate_rejected"
        )
        result.payloads[key] = dup_body

    # 5. One item lookup.
    if items and items[0].get("payout_item_id"):
        try:
            item_id = str(items[0]["payout_item_id"])
            result.payloads["item"] = await client.get_item(item_id)
        except STEP_ERRORS as exc:
            result.checks.append(Check("item_lookup", False, type(exc).__name__))

    # 6. Best-effort webhook event for this batch.
    event = await client.latest_webhook_event(EVENT_TYPE_ITEM_SUCCEEDED, str(batch_id))
    if event is not None:
        result.payloads["webhook_item_succeeded"] = event
    return result


def save_fixtures(
    payloads: dict[str, Any], recipients: list[str], directory: Path | None = None
) -> list[str]:
    """Write redacted payloads; any email left after redaction becomes a placeholder."""
    target = directory if directory is not None else FIXTURE_DIR
    target.mkdir(parents=True, exist_ok=True)
    keep = {f"sb-member{i}@personal.example.com" for i in range(1, len(recipients) + 1)}

    def backstop(match: re.Match[str]) -> str:
        return match.group(0) if match.group(0) in keep else "redacted@example.com"

    saved: list[str] = []
    for key, payload in payloads.items():
        text = json.dumps(redact(payload, recipients), indent=2, ensure_ascii=False)
        text = _EMAIL_RE.sub(backstop, text)
        path = target / f"payouts_{key}.json"
        path.write_text(text + "\n", encoding="utf-8")
        saved.append(path.name)
    return saved


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    def amount_type(text: str) -> Decimal:
        try:
            value = parse_amount(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
        if value > MAX_AMOUNT:
            raise argparse.ArgumentTypeError(f"amount must not exceed {MAX_AMOUNT}")
        return value

    def currency_type(text: str) -> str:
        if not _CURRENCY_RE.fullmatch(text):
            raise argparse.ArgumentTypeError("currency must be 3 uppercase letters")
        return text

    parser = argparse.ArgumentParser(
        description="Payouts L1 sandbox proof (2-item batch)."
    )
    parser.add_argument("--to", action="append", default=[], metavar="EMAIL")
    parser.add_argument("--amount", type=amount_type, default=Decimal("1.00"))
    parser.add_argument("--currency", type=currency_type, default="USD")
    parser.add_argument("--save-fixtures", action="store_true")
    args = parser.parse_args(argv)
    if len(args.to) != 2:
        parser.error("exactly 2 --to recipients are required")
    if any("@" not in email for email in args.to):
        parser.error("--to must be an email address")
    if len({email.strip().lower() for email in args.to}) != 2:
        parser.error("the two --to recipients must be different")
    return args


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    recipients: list[str] = [email.strip().lower() for email in args.to]
    run_id = str(ULID()).lower()
    async with httpx.AsyncClient() as http:
        client = PayoutsClient(
            get_settings().paypal_api_base_url,
            get_auth_manager(),
            http,
            sleep=asyncio.sleep,
        )
        try:
            result = await run_l1(
                client, recipients, args.amount, args.currency, run_id
            )
        except STEP_ERRORS as exc:
            print(f"FAIL request: {type(exc).__name__}")
            return 1
    for check in result.checks:
        print(f"{'PASS' if check.passed else 'FAIL'} {check.name}: {check.detail}")
    if args.save_fixtures:
        for name in save_fixtures(result.payloads, recipients):
            print(f"saved {name}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(main()))

"""Invoicing v2 draft creation: the invoice id comes from either response shape."""

from __future__ import annotations

from typing import Any

import pytest

from backend.app.ai.paypal_tools import (
    BillingInfo,
    CreateInvoiceParameters,
    InvoiceItem,
    Money,
    PrimaryRecipient,
)
from backend.app.services.paypal import invoices
from backend.app.services.paypal.invoices import InvoicingExecutor, draft_invoice_id

HREF = "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-ABCD-1234-EFGH-5678"


def params() -> CreateInvoiceParameters:
    return CreateInvoiceParameters(
        currency_code="USD",
        reference="ms_seed_01",
        note="Clause 4.2: late payment penalty",
        primary_recipients=[
            PrimaryRecipient(billing_info=BillingInfo(email_address="client@acme.com"))
        ],
        items=[
            InvoiceItem(
                name="Shortfall",
                quantity="1",
                unit_amount=Money(currency_code="USD", value="500.00"),
            )
        ],
    )


async def create_with(monkeypatch: pytest.MonkeyPatch, document: dict[str, Any]) -> str:
    calls: list[tuple[str, str]] = []

    async def fake_request(method: str, path: str, **_: Any) -> dict[str, Any]:
        calls.append((method, path))
        return document

    monkeypatch.setattr(invoices, "paypal_request", fake_request)
    executor = InvoicingExecutor(discrepancy_id="dsc_01JTESTULID0000000000", attempt=1)
    invoice_id = await executor.create_invoice(params(), request_id="dsc_01:remediate")
    assert calls == [("POST", invoices.INVOICES_PATH)]
    return invoice_id


@pytest.mark.asyncio
async def test_full_representation_uses_id(monkeypatch: pytest.MonkeyPatch) -> None:
    got = await create_with(monkeypatch, {"id": " INV2-FULL-0001 ", "status": "DRAFT"})
    assert got == "INV2-FULL-0001"


@pytest.mark.asyncio
async def test_link_response_uses_href(monkeypatch: pytest.MonkeyPatch) -> None:
    # PayPal's default (minimal) create response: a single HATEOAS link.
    document = {"rel": "self", "href": HREF, "method": "GET"}
    assert await create_with(monkeypatch, document) == "INV2-ABCD-1234-EFGH-5678"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        {},
        {"id": "   "},
        {"rel": "self", "href": "", "method": "GET"},
        {"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/"},
        {"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/O-1"},
        {"rel": "send", "href": HREF + "/send", "method": "POST"},
        {"rel": "self", "href": 42},
    ],
)
async def test_malformed_response_still_raises(
    monkeypatch: pytest.MonkeyPatch, document: dict[str, Any]
) -> None:
    with pytest.raises(KeyError, match="no invoice id"):
        await create_with(monkeypatch, document)


def test_href_with_trailing_slash() -> None:
    assert draft_invoice_id({"href": HREF + "/"}) == "INV2-ABCD-1234-EFGH-5678"

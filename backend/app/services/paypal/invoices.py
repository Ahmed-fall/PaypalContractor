"""Invoicing v2 remediation service: the ``PayPalInvoiceExecutor`` P2's engine calls.

PIT-01: the AI citation goes in ``detail.note`` and the breakdown in ``items[]``
at DRAFT creation — never via the ``/send`` body, which PayPal ignores.
PIT-11: the invoice number is derived from the discrepancy ULID, never a counter.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ...ai.paypal_tools import (
    CreateInvoiceParameters,
    GenerateInvoiceQrCodeParameters,
    SendInvoiceParameters,
)
from ._client import paypal_request

INVOICES_PATH = "/v2/invoicing/invoices"


def invoice_number(discrepancy_id: str, attempt: int) -> str:
    """PIT-11: ``FD-<last 10 of discrepancy ULID>-R<attempt>``; deleted drafts may
    still hold their number at PayPal, so a new attempt also gets a new number."""
    return f"FD-{discrepancy_id[-10:].upper()}-R{attempt}"


def _body(params: CreateInvoiceParameters, number: str) -> dict[str, Any]:
    billing = (
        params.primary_recipients[0].billing_info
        if params.primary_recipients
        else None
    )
    body: dict[str, Any] = {
        "detail": {
            "currency_code": params.currency_code,
            "reference": params.reference,
            "note": params.note or "",
            "invoice_number": number,
        },
        "items": [
            {
                "name": item.name,
                "description": item.description,
                "quantity": item.quantity,
                "unit_amount": {
                    "currency_code": params.currency_code,
                    "value": item.unit_amount.value,
                },
            }
            for item in params.items
        ],
    }
    if billing is not None:
        billing_info: dict[str, Any] = {"email_address": str(billing.email_address)}
        if billing.business_name:
            billing_info["business_name"] = billing.business_name
        body["primary_recipients"] = [{"billing_info": billing_info}]
    return body


def payer_view_url(document: dict[str, Any]) -> str | None:
    for link in document.get("links", []):
        if link.get("rel") == "payer_view":
            return str(link["href"])
        if link.get("rel") == "payer-view":
            return str(link["href"])
    return None


def draft_invoice_id(document: dict[str, Any]) -> str:
    """The new draft's id from an Invoicing v2 create response.

    Without ``Prefer: return=representation`` PayPal answers ``{rel, href, method}``
    instead of the invoice body; the id is then the last segment of ``href``
    (``.../v2/invoicing/invoices/INV2-XXXX``).
    """
    invoice_id = document.get("id")
    if isinstance(invoice_id, str) and invoice_id.strip():
        return invoice_id.strip()
    href = document.get("href")
    if isinstance(href, str):
        prefix, _, tail = urlsplit(href.strip()).path.rstrip("/").rpartition("/")
        if prefix.endswith(INVOICES_PATH) and tail:
            return tail
    raise KeyError("PayPal draft response has no invoice id")


class InvoicingExecutor:
    """One instance per remediation attempt: carries the PIT-11 context."""

    def __init__(self, *, discrepancy_id: str, attempt: int) -> None:
        self._discrepancy_id = discrepancy_id
        self._attempt = attempt

    async def create_invoice(
        self, params: CreateInvoiceParameters, request_id: str
    ) -> str:
        number = invoice_number(self._discrepancy_id, self._attempt)
        body = _body(params, number)
        document = await paypal_request(
            "POST", INVOICES_PATH, request_id=request_id, json_body=body
        )
        return draft_invoice_id(document)

    async def send_invoice(self, params: SendInvoiceParameters) -> None:
        await paypal_request(
            "POST",
            f"{INVOICES_PATH}/{params.invoice_id}/send",
            request_id=f"{params.invoice_id}:send",
            json_body={"send_to_recipient": True, "send_to_invoicer": True},
        )

    async def generate_invoice_qr_code(
        self, params: GenerateInvoiceQrCodeParameters
    ) -> str:
        document = await paypal_request(
            "POST",
            f"{INVOICES_PATH}/{params.invoice_id}/generate-qr-code",
            request_id=f"{params.invoice_id}:qr",
            json_body={
                "width": params.width,
                "height": params.height,
                "content_type": "image/png",
            },
        )
        image = document.get("image_as_base64") or document.get("image")
        if not isinstance(image, str) or not image:
            return ""
        return image


async def get_invoice(invoice_id: str) -> dict[str, Any]:
    return await paypal_request(
        "GET",
        f"{INVOICES_PATH}/{invoice_id}",
        request_id=f"{invoice_id}:get",
    )


async def delete_draft(invoice_id: str) -> None:
    """R3: compensate a rejected resurrection by deleting the stale draft."""
    await paypal_request(
        "DELETE",
        f"{INVOICES_PATH}/{invoice_id}",
        request_id=f"{invoice_id}:delete",
    )


async def fetch_payer_view(invoice_id: str) -> str | None:
    return payer_view_url(await get_invoice(invoice_id))

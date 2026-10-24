"""PayPal Agent Toolkit–compatible invoice tool parameters, built by the engine.

The shapes mirror ``paypal-agent-toolkit`` 1.11.0
(``paypal_agent_toolkit/shared/invoices/parameters.py``) with the exact upstream field
names, so a tool call made here means the same thing to the toolkit and to PayPal. The
package itself is not a dependency: it requires ``pydantic>=2.10`` plus langchain,
crewai-tools and openai-agents. Only the fields this engine fills are declared.

Upstream maps ``CreateInvoiceParameters.note`` to the invoice's ``detail.note`` at
draft creation, which is where PayPal keeps it (``/send`` notes are ignored, PIT-01).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..schemas.contracts import DiscrepancyEvaluationResult, ExtractedMilestoneSpec
from .auditor_agent import FOOTER_AMOUNT_PREFIX, FOOTER_CLAUSE_PREFIX, ContractTerms

AGENT_TOOLKIT_VERSION = "1.11.0"
# PayPal Invoicing v2 limits (developer.paypal.com/api/invoicing/v2/schema.json).
PAYPAL_NOTE_MAX_CHARS = 4000  # detail.note
PAYPAL_REFERENCE_MAX = 120  # detail.reference
PAYPAL_BUSINESS_NAME_MAX = 300  # billing_info.business_name

# The remediation sequence, in execution order; the reminder is offered separately.
REMEDIATION_TOOL_NAMES: tuple[str, ...] = (
    "create_invoice",
    "send_invoice",
    "generate_invoice_qr_code",
)
REMINDER_TOOL_NAME = "send_invoice_reminder"


class NothingToInvoice(ValueError):
    """The evaluation has no amount to bill (compliant, or a currency mismatch)."""


class InvoiceNoteTooLong(ValueError):
    """The invoice note would exceed PayPal's 4000-character ``detail.note`` limit."""


class InvoiceInvariantError(ValueError):
    """The evaluation's line items do not form a valid invoice for its total.

    Never expected from the current auditor; guards the PayPal call if the math or the
    line-item builder ever changes.
    """


class _ToolModel(BaseModel):
    """Unknown fields are an error, not silently dropped: a model-proposed call that
    adds anything the engine did not set (a recipient, a discount, partial payment)
    must fail validation rather than compare equal."""

    model_config = ConfigDict(extra="forbid")


class Money(_ToolModel):
    currency_code: str
    value: str


class InvoiceItem(_ToolModel):
    name: str
    description: str | None = None
    quantity: str
    unit_amount: Money


class BillingInfo(_ToolModel):
    business_name: str | None = None
    email_address: str | None = None


class PrimaryRecipient(_ToolModel):
    billing_info: BillingInfo | None = None


class CreateInvoiceParameters(_ToolModel):
    currency_code: str
    reference: str | None = None
    note: str | None = None
    primary_recipients: list[PrimaryRecipient]
    items: list[InvoiceItem]


class SendInvoiceParameters(_ToolModel):
    invoice_id: str
    send_to_recipient: bool | None = True


class SendInvoiceReminderParameters(_ToolModel):
    invoice_id: str
    subject: str | None = None
    note: str | None = None


class GenerateInvoiceQrCodeParameters(_ToolModel):
    invoice_id: str
    width: int = 300
    height: int = 300


def engine_note_footer(evaluation: DiscrepancyEvaluationResult) -> str:
    """The engine-written tail of the invoice note (verbatim clause + amount due).

    The auditor appends the footer last, after a blank line, starting with the clause
    line when a clause was violated and with the amount line otherwise. Knowing which
    line it starts with, and taking the *last* such paragraph, means marker-like lines
    in an AI-written body can never be mistaken for it. Returns ``""`` if absent.
    """
    note = evaluation.paypal_invoice_detail_note
    marker = (
        FOOTER_CLAUSE_PREFIX
        if evaluation.violated_clause_quote
        else FOOTER_AMOUNT_PREFIX
    )
    start = note.rfind("\n\n" + marker)
    if start >= 0:
        return note[start + 2 :]
    return note if note.startswith(marker) else ""


def _invoice_note(evaluation: DiscrepancyEvaluationResult, override: str | None) -> str:
    if override is None or not override.strip():
        return evaluation.paypal_invoice_detail_note
    footer = engine_note_footer(evaluation)
    note = f"{override.strip()}\n\n{footer}" if footer else override.strip()
    if len(note) > PAYPAL_NOTE_MAX_CHARS:
        raise InvoiceNoteTooLong(
            f"invoice note is {len(note)} characters; PayPal allows {PAYPAL_NOTE_MAX_CHARS}"
        )
    return note


def _check_invoice_invariants(
    currency: str, evaluation: DiscrepancyEvaluationResult
) -> None:
    items = evaluation.remediation_line_items
    total = sum((item.unit_amount_value for item in items), Decimal("0.00"))
    if total != evaluation.total_remediation_amount:
        raise InvoiceInvariantError(
            f"line items sum to {total}, expected {evaluation.total_remediation_amount}"
        )
    for item in items:
        if item.unit_amount_value <= 0:
            raise InvoiceInvariantError(f"line item {item.name!r} is not positive")
        if item.currency_code != currency:
            raise InvoiceInvariantError(
                f"line item {item.name!r} is in {item.currency_code}, invoice is {currency}"
            )


def build_create_invoice_params(
    contract: ContractTerms,
    milestone: ExtractedMilestoneSpec,
    evaluation: DiscrepancyEvaluationResult,
    *,
    note_override: str | None = None,
) -> CreateInvoiceParameters:
    """The canonical ``create_invoice`` arguments for one remediation.

    Every amount comes from ``evaluation.remediation_line_items``, which the
    deterministic math produced. A non-blank ``note_override`` (the approver's own
    text) replaces the note body; the engine footer is always kept.
    """
    if (
        evaluation.total_remediation_amount <= 0
        or not evaluation.remediation_line_items
    ):
        raise NothingToInvoice(
            f"{milestone.milestone_code} has nothing to invoice "
            f"({evaluation.discrepancy_type.value})"
        )
    _check_invoice_invariants(contract.currency, evaluation)
    return CreateInvoiceParameters(
        currency_code=contract.currency,
        reference=milestone.milestone_code[:PAYPAL_REFERENCE_MAX],
        note=_invoice_note(evaluation, note_override),
        primary_recipients=[
            PrimaryRecipient(
                billing_info=BillingInfo(
                    business_name=contract.client_name[:PAYPAL_BUSINESS_NAME_MAX],
                    email_address=str(contract.client_paypal_email),
                )
            )
        ],
        items=[
            InvoiceItem(
                name=item.name,
                description=item.description,
                quantity=item.quantity,
                unit_amount=Money(
                    currency_code=item.currency_code,
                    value=f"{item.unit_amount_value:.2f}",
                ),
            )
            for item in evaluation.remediation_line_items
        ],
    )


# --- Gemini function declarations ------------------------------------------------------

_ENGINE_SUPPLIES = (
    " The compliance engine supplies every argument value (amounts, currency, recipient,"
    " line items, note); copy the arguments you are given unchanged."
)
_TOOL_DESCRIPTIONS: dict[str, tuple[str, type[BaseModel]]] = {
    "create_invoice": (
        "Create a draft PayPal invoice for the approved remediation (step 1 of 3)."
        + _ENGINE_SUPPLIES,
        CreateInvoiceParameters,
    ),
    "send_invoice": (
        "Send the draft invoice created in step 1 to the client (step 2 of 3).",
        SendInvoiceParameters,
    ),
    "generate_invoice_qr_code": (
        "Generate the payment QR code for the sent invoice (step 3 of 3).",
        GenerateInvoiceQrCodeParameters,
    ),
    REMINDER_TOOL_NAME: (
        "Send a payment reminder for an invoice that was already sent.",
        SendInvoiceReminderParameters,
    ),
}


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` with every ``$ref`` replaced by its ``$defs`` entry."""
    defs: dict[str, Any] = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                return resolve(defs[ref.rsplit("/", 1)[-1]])
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    resolved: dict[str, Any] = resolve(schema)
    return resolved


def tool_declarations() -> list[dict[str, Any]]:
    """Agent Toolkit tools as self-contained Gemini function declarations."""
    return [
        {
            "name": name,
            "description": description,
            "parameters": _inline_refs(model.model_json_schema()),
        }
        for name, (description, model) in _TOOL_DESCRIPTIONS.items()
    ]

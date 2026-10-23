"""AI & compliance engine (owned by Person 2).

Stateless library imported by the backend. Public functions:

- ``extract_contract_rules(file_bytes, filename)`` -> ``ContractExtractionResult``
  Raises ``ContractParseError`` (map to HTTP 422) for unusable uploads.
- ``evaluate_payment_compliance(contract, milestone, captured_amount, captured_currency,
  captured_date, payer_note="")`` -> ``DiscrepancyEvaluationResult``
  Never raises on model failure; money always comes from ``compliance_math``.
- ``run_remediation(contract, milestone, evaluation, executor, *, discrepancy_id,
  confirm_send, note_override=None)`` -> ``RemediationRun``
  Creates, sends and QR-codes the PayPal remediation invoice through a
  ``PayPalInvoiceExecutor`` that the backend (P1) implements; P2 never calls PayPal.
  Gemini starts the workflow by PayPal Agent Toolkit–compatible tool calls, but only
  the engine's own arguments are ever executed.

Only ``GEMINI_API_KEY`` is read (optionally ``GEMINI_MODEL`` / ``GEMINI_FALLBACK_MODEL``);
without it every function still works through its deterministic fallback.
"""

from .auditor_agent import evaluate_payment_compliance
from .extractor import extract_contract_rules
from .paypal_tools import (
    InvoiceInvariantError,
    InvoiceNoteTooLong,
    NothingToInvoice,
    build_create_invoice_params,
)
from .pdf_parser import ContractParseError
from .remediation_agent import (
    PayPalInvoiceExecutor,
    RemediationNotApproved,
    RemediationRun,
    RemediationStepFailed,
    remediation_request_id,
    run_remediation,
)

__all__ = [
    "ContractParseError",
    "InvoiceInvariantError",
    "InvoiceNoteTooLong",
    "NothingToInvoice",
    "PayPalInvoiceExecutor",
    "RemediationNotApproved",
    "RemediationRun",
    "RemediationStepFailed",
    "build_create_invoice_params",
    "evaluate_payment_compliance",
    "extract_contract_rules",
    "remediation_request_id",
    "run_remediation",
]

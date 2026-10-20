"""Canonical interface contracts shared by the backend, the AI engine, and the frontend.

Money is ``decimal.Decimal`` serialized as a string; ``float`` is banned on financial
paths. ``DiscrepancyType`` (immutable audit reason) and ``DiscrepancyStatus``
(remediation lifecycle) are deliberately orthogonal — remediation changes status only.
The AI never computes money; it extracts parameters and writes prose only.
``EmailStr`` requires the ``email-validator`` dependency.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field


class ContractStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class DiscrepancyType(str, Enum):
    COMPLIANT = "COMPLIANT"
    UNDERPAID = "UNDERPAID"
    LATE_PAYMENT = "LATE_PAYMENT"
    UNDERPAID_AND_LATE = "UNDERPAID_AND_LATE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"


class DiscrepancyStatus(str, Enum):
    NONE = "NONE"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    REMEDIATED = "REMEDIATED"


class ExtractedMilestoneSpec(BaseModel):
    milestone_code: str = Field(..., description="Short contract code e.g. MS-01")
    title: str
    expected_amount: Decimal = Field(..., decimal_places=2)
    due_date: date
    source_clause_quote: str = Field(..., description="Verbatim quote from contract")


class PersistedMilestoneSpec(ExtractedMilestoneSpec):
    milestone_id: str = Field(..., description="Backend-assigned canonical ID e.g. ms_01J9Z8A")


class ContractExtractionResult(BaseModel):
    """Output of ``extract_contract_rules(file_bytes, filename)``; no database IDs."""

    contract_title: str
    client_name: str
    client_paypal_email: EmailStr
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    late_fee_percentage: Decimal = Field(default=Decimal("0.00"), decimal_places=2)
    grace_period_days: int = Field(default=0, ge=0)
    allow_partial_payments: bool = Field(default=False)
    penalty_clause_quote: str
    milestones: List[ExtractedMilestoneSpec]
    extraction_confidence: float = Field(..., ge=0.0, le=1.0)


class PersistedContractExtraction(BaseModel):
    contract_title: str
    client_name: str
    client_paypal_email: EmailStr
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    late_fee_percentage: Decimal = Field(default=Decimal("0.00"), decimal_places=2)
    grace_period_days: int = Field(default=0, ge=0)
    allow_partial_payments: bool = Field(default=False)
    penalty_clause_quote: str
    milestones: List[PersistedMilestoneSpec]
    extraction_confidence: float = Field(..., ge=0.0, le=1.0)


class InvoiceLineItemSpec(BaseModel):
    name: str = Field(..., max_length=120)
    description: str = Field(..., max_length=500)
    quantity: str = "1"
    unit_amount_value: Decimal = Field(..., decimal_places=2)
    currency_code: str = "USD"


class DiscrepancyEvaluationResult(BaseModel):
    discrepancy_type: DiscrepancyType
    discrepancy_status: DiscrepancyStatus
    is_compliant: bool
    expected_amount: Decimal
    captured_amount: Decimal
    captured_currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")
    shortfall_amount: Decimal
    penalty_amount: Decimal
    total_remediation_amount: Decimal
    violated_clause_quote: str
    ai_audit_summary: str = Field(..., max_length=600)
    paypal_invoice_detail_note: str = Field(
        ...,
        max_length=1500,
        description="Set on invoice draft creation; /send ignores body notes.",
    )
    remediation_line_items: List[InvoiceLineItemSpec]


class ContractExtractResponse(BaseModel):
    contract_id: str
    status: ContractStatus
    source_filename: str
    extraction: PersistedContractExtraction


class ContractActivateRequest(BaseModel):
    contract_title: str
    client_name: str
    client_paypal_email: EmailStr
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    late_fee_percentage: Decimal = Field(default=Decimal("0.00"), decimal_places=2)
    grace_period_days: int = Field(default=0, ge=0)
    allow_partial_payments: bool = Field(default=False)
    penalty_clause_quote: str
    milestones: List[PersistedMilestoneSpec]


class ContractActivateResponse(BaseModel):
    contract_id: str
    status: ContractStatus
    milestones: List[PersistedMilestoneSpec]


class RemediationInvoiceRecord(BaseModel):
    paypal_invoice_id: str
    paypal_invoice_number: str
    status: str = Field(..., description="PayPal Invoice status e.g. SENT")
    total_amount: Decimal
    currency: str
    recipient_email: EmailStr
    payer_view_url: str
    qr_code_base64: str
    sent_at: datetime
    last_reminded_at: Optional[datetime] = None


class LedgerRow(BaseModel):
    milestone_id: str
    contract_id: str
    contract_title: str
    client_name: str
    client_paypal_email: EmailStr
    milestone_code: str
    milestone_title: str
    expected_amount: Decimal
    captured_amount: Optional[Decimal] = None
    captured_currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")
    shortfall_amount: Decimal = Decimal("0.00")
    penalty_amount: Decimal = Decimal("0.00")
    total_remediation_amount: Decimal = Decimal("0.00")
    currency: str = "USD"
    due_date: date
    captured_at: Optional[datetime] = None
    paypal_order_id: Optional[str] = None
    paypal_capture_id: Optional[str] = None
    webhook_verified: bool = False
    discrepancy_type: Optional[DiscrepancyType] = None
    discrepancy_status: DiscrepancyStatus = DiscrepancyStatus.NONE
    discrepancy_id: Optional[str] = None
    violated_clause_quote: Optional[str] = None
    ai_audit_summary: Optional[str] = None
    remediation_invoice: Optional[RemediationInvoiceRecord] = None


class LedgerSummary(BaseModel):
    total_expected: Decimal
    total_captured: Decimal
    total_shortfall: Decimal
    total_penalties: Decimal
    total_remediation_invoiced: Decimal


class LedgerResponse(BaseModel):
    summary: LedgerSummary
    rows: List[LedgerRow]


class SimulatorPaymentRequest(BaseModel):
    milestone_id: str
    simulated_amount: Decimal = Field(..., decimal_places=2)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    simulated_payment_date: date
    payer_email: EmailStr
    idempotency_key: Optional[str] = None


class SimulatorPaymentResponse(BaseModel):
    paypal_order_id: str
    paypal_capture_id: str
    evaluation: DiscrepancyEvaluationResult


class RemediationRequest(BaseModel):
    confirm_send: bool = Field(default=False, description="Required to create and send the invoice.")
    custom_note_override: Optional[str] = None
    idempotency_key: Optional[str] = None


class RemediationResponse(BaseModel):
    discrepancy_id: str
    discrepancy_status: DiscrepancyStatus
    remediation_invoice: RemediationInvoiceRecord


class RemindResponse(BaseModel):
    discrepancy_id: str
    remediation_invoice: RemediationInvoiceRecord


class HealthResponse(BaseModel):
    status: str = "ok"
    paypal_sandbox_connected: bool

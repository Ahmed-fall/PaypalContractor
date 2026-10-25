"""Canonical interface contracts shared by the backend, the AI engine, and the frontend.

Money is ``decimal.Decimal`` serialized as a string; ``float`` is banned on financial
paths. ``DiscrepancyType`` (immutable audit reason) and ``DiscrepancyStatus``
(remediation lifecycle) are deliberately orthogonal — remediation changes status only.
The AI never computes money; it extracts parameters and writes prose only.
``EmailStr`` requires the ``email-validator`` dependency.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, List, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


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
    BLOCKED = "BLOCKED"
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
    leash_decision: LeashDecision | None = None
    remediation_sent_by: RemediationSentBy | None = None


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
    capture_status: Literal["COMPLETED"] = "COMPLETED"
    discrepancy_id: Optional[str] = None
    evaluation: DiscrepancyEvaluationResult
    leash: GuardResult | None = None
    remediation_invoice: RemediationInvoiceRecord | None = None
    payroll_run_id: str | None = None


class RemediationRequest(BaseModel):
    confirm_send: bool = Field(
        default=False, description="Required to create and send the invoice."
    )
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
    leash_enabled: bool = True
    ai_provider: Optional[str] = None


# --- Leash (Rev 2.1 §7.5 Interface 2; owner P5, approved by P4) ---


class LeashDecision(str, Enum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


class GuardAction(str, Enum):
    SEND_REMEDIATION_INVOICE = "SEND_REMEDIATION_INVOICE"
    PAY_TEAM_MEMBER = "PAY_TEAM_MEMBER"
    SEND_PAYROLL_BATCH = "SEND_PAYROLL_BATCH"


PayrollMode = Literal["MILESTONE", "MONTHLY"]
PayrollRunStatus = Literal[
    "PREPARED", "CHECKED", "AWAITING_APPROVAL", "SENT", "PAID", "PARTIAL", "FAILED"
]
PayoutStatus = Literal["PENDING", "SUCCESS", "UNCLAIMED", "FAILED"]
OwnerAction = Literal["APPROVED", "EXCLUDED"]
# "rate" goes beyond the plan comment: A.3 rate_in_file feeds PR-04.
TimesheetField = Literal["member", "work_date", "hours", "milestone_code", "rate"]
RuleConditionValue = bool | int | float | str
_MONEY_2DP = re.compile(r"[0-9]+\.[0-9]{2}")


class LeashRule(BaseModel):
    id: str = Field(..., min_length=1, max_length=16)
    applies_to: GuardAction
    when: dict[str, RuleConditionValue]
    outcome: LeashDecision
    source_text: str

    @field_validator("when")
    @classmethod
    def _amount_thresholds_are_decimal_strings(
        cls, v: dict[str, RuleConditionValue]
    ) -> dict[str, RuleConditionValue]:
        msg = "amount thresholds must be 2-dp decimal strings"
        for key, value in v.items():
            if "amount" not in key:
                continue
            if not isinstance(value, str) or not _MONEY_2DP.fullmatch(value):
                raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _allow_needs_a_condition(self) -> LeashRule:
        if self.outcome == LeashDecision.ALLOW and not self.when:
            msg = "an ALLOW rule needs at least one condition"
            raise ValueError(msg)
        return self


class LeashPolicy(BaseModel):
    version: int = Field(..., ge=1)
    rules: list[LeashRule]
    payroll_margin_limit_pct: Decimal = Field(default=Decimal(80), gt=0, le=100)
    email_change_window_days: int = Field(default=7, ge=0)


class GuardResult(BaseModel):
    decision: LeashDecision
    rule_ids: list[str]
    reason: str = Field(..., max_length=200)
    decision_id: str


class ColumnMappingProposal(BaseModel):
    mapping: dict[str, TimesheetField]
    confidence: dict[str, Annotated[float, Field(ge=0, le=1)]]


class PayrollLineOut(BaseModel):
    line_id: str
    member_name: str
    payout_email: EmailStr
    hours: Decimal = Field(..., decimal_places=2)
    rate: Decimal = Field(..., decimal_places=2)
    gross: Decimal = Field(..., decimal_places=2)
    decision: LeashDecision
    flags: list[str]
    reason: str | None = Field(default=None, max_length=200)
    owner_action: OwnerAction | None = None
    payout_status: PayoutStatus | None = None


class PayrollRunOut(BaseModel):
    run_id: str
    mode: PayrollMode
    milestone_id: str | None = None
    period: str | None = None
    status: PayrollRunStatus
    total: Decimal = Field(..., decimal_places=2)
    cash_in: Decimal = Field(..., decimal_places=2)
    cash_used_pct: Decimal = Field(..., decimal_places=2)
    run_decision: LeashDecision
    run_flags: list[str]
    lines: list[PayrollLineOut]
    sender_batch_id: str | None = None


# --- Leash REST models (Rev 2.1 A.2.2) and hook H1/H2 response fields ---

DecisionSubjectType = Literal["discrepancy", "payroll_line", "payroll_run"]
DecisionActor = Literal["leash", "owner"]
RemediationSentBy = Literal["owner", "leash-auto"]


class PolicyCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=4000)


class PolicyCompileResponse(BaseModel):
    draft_version: int
    status: Literal["DRAFT"]
    policy: LeashPolicy
    cards: list[str]
    test_cases: list[str]


class PolicyActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(..., ge=1)


class PolicyActivateResponse(BaseModel):
    version: int
    active: bool
    activated_at: datetime


class ActiveLeashPolicy(LeashPolicy):
    activated_at: datetime


class DecisionLogEntry(BaseModel):
    decision_id: str
    created_at: datetime
    policy_version: int
    action_type: GuardAction
    subject_type: DecisionSubjectType
    subject_id: str
    decision: LeashDecision
    rule_ids: list[str]
    amount: Decimal | None = None
    reason: str = Field(..., max_length=200)
    actor: DecisionActor


class DecisionLogResponse(BaseModel):
    items: list[DecisionLogEntry]
    next_cursor: str | None = None


class TeamMemberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    payout_email: EmailStr
    hourly_rate: Decimal = Field(..., gt=0, decimal_places=2, max_digits=8)

    @field_validator("payout_email", mode="after")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        return v.strip().lower()


class TeamMemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    payout_email: EmailStr | None = None
    hourly_rate: Decimal | None = Field(
        default=None, gt=0, decimal_places=2, max_digits=8
    )
    active: bool | None = None

    @field_validator("payout_email", mode="after")
    @classmethod
    def _normalise_email(cls, v: str | None) -> str | None:
        return None if v is None else v.strip().lower()

    @model_validator(mode="after")
    def _no_nulls_or_empty_body(self) -> TeamMemberUpdate:
        if not self.model_fields_set:
            msg = "at least one field must be provided"
            raise ValueError(msg)
        if any(getattr(self, f) is None for f in self.model_fields_set):
            msg = "fields may be omitted but not null"
            raise ValueError(msg)
        return self


class TeamMemberOut(BaseModel):
    member_id: str
    name: str
    payout_email: EmailStr
    hourly_rate: Decimal
    email_changed_at: datetime | None = None
    first_paid_at: datetime | None = None
    active: bool


class TimesheetMappingRequired(BaseModel):
    upload_id: str
    status: Literal["MAPPING_REQUIRED"]
    row_count: int
    headers: list[str]
    proposal: ColumnMappingProposal


class TimesheetMappingConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mapping: dict[str, TimesheetField]

    @field_validator("mapping")
    @classmethod
    def _targets_are_unique(cls, v: dict[str, TimesheetField]) -> dict[str, TimesheetField]:
        if len(set(v.values())) != len(v):
            msg = "each timesheet field may be mapped from only one column"
            raise ValueError(msg)
        return v


class TimesheetParsed(BaseModel):
    upload_id: str
    status: Literal["PARSED"]
    entries: int
    unknown_members: list[str]
    milestones: list[str]
    warnings: list[str]


class PayrollRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PayrollMode
    milestone_id: str | None = None
    period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")

    @model_validator(mode="after")
    def _mode_selects_key(self) -> PayrollRunCreate:
        if self.mode == "MILESTONE":
            if self.milestone_id is None or self.period is not None:
                msg = "MILESTONE runs require milestone_id and no period"
                raise ValueError(msg)
        elif self.period is None or self.milestone_id is not None:
            msg = "MONTHLY runs require period and no milestone_id"
            raise ValueError(msg)
        return self


class PayrollLineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_action: OwnerAction
    note: str | None = Field(default=None, max_length=200)


class PayrollSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]
    approve_run_flags: list[str] = Field(default_factory=list)
    # Required: this endpoint moves money.
    idempotency_key: str = Field(..., min_length=1, max_length=64)

    @field_validator("confirm", mode="before")
    @classmethod
    def _confirm_is_boolean_true(cls, v: object) -> object:
        # Literal[True] alone would coerce 1 to True; money moves need a real boolean.
        if v is not True:
            msg = "confirm must be the boolean true"
            raise ValueError(msg)
        return v


class PayrollSendResponse(BaseModel):
    run_id: str
    status: Literal["SENT"]
    sender_batch_id: str
    paypal_batch_id: str
    batch_status: str
    total: Decimal
    lines_sent: int
    lines_excluded: int


class MarginRow(BaseModel):
    milestone_id: str
    milestone_code: str
    cash_in: Decimal
    invoiced_open: Decimal
    paid_to_team: Decimal
    pending_to_team: Decimal
    margin: Decimal
    margin_pct: Decimal
    payroll_status: PayrollRunStatus | None = None


class MarginSummary(BaseModel):
    cash_in: Decimal
    paid_to_team: Decimal
    margin: Decimal


class MarginResponse(BaseModel):
    rows: list[MarginRow]
    summary: MarginSummary


# GuardResult / LeashDecision / RemediationSentBy are defined after these classes.
SimulatorPaymentResponse.model_rebuild()
LedgerRow.model_rebuild()

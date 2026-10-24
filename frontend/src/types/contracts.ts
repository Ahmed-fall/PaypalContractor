/**
 * TypeScript mirror of backend/app/schemas/contracts.py.
 *
 * Money is Decimal serialized as a string (e.g. "1000.00"), never a JS number.
 * `DiscrepancyType` (audit reason) and `DiscrepancyStatus` (remediation lifecycle)
 * are orthogonal. No `any` is permitted in this file.
 */

export type ContractStatus = "DRAFT" | "ACTIVE" | "ARCHIVED";

export type DiscrepancyType =
  | "COMPLIANT"
  | "UNDERPAID"
  | "LATE_PAYMENT"
  | "UNDERPAID_AND_LATE"
  | "CURRENCY_MISMATCH";

export type DiscrepancyStatus = "NONE" | "PENDING_APPROVAL" | "BLOCKED" | "REMEDIATED";

export interface ExtractedMilestoneSpec {
  milestone_code: string;
  title: string;
  expected_amount: string; // Decimal serialized as "1000.00"
  due_date: string; // ISO date "YYYY-MM-DD"
  source_clause_quote: string;
}

export interface PersistedMilestoneSpec extends ExtractedMilestoneSpec {
  milestone_id: string; // Assigned by backend on /extract or /activate
}

export interface PersistedContractExtraction {
  contract_title: string;
  client_name: string;
  client_paypal_email: string;
  currency: string;
  late_fee_percentage: string;
  grace_period_days: number;
  allow_partial_payments: boolean;
  penalty_clause_quote: string;
  milestones: PersistedMilestoneSpec[];
  extraction_confidence: number;
}

export interface ContractExtractResponse {
  contract_id: string;
  status: ContractStatus;
  source_filename: string;
  extraction: PersistedContractExtraction;
}

export type ContractActivateRequest = Omit<PersistedContractExtraction, "extraction_confidence">;

export interface ContractActivateResponse {
  contract_id: string;
  status: ContractStatus;
  milestones: PersistedMilestoneSpec[];
}

export interface RemediationInvoiceRecord {
  paypal_invoice_id: string;
  paypal_invoice_number: string;
  status: string;
  total_amount: string;
  currency: string;
  recipient_email: string;
  payer_view_url: string;
  qr_code_base64: string;
  sent_at: string;
  last_reminded_at?: string | null;
}

export interface LedgerRow {
  milestone_id: string;
  contract_id: string;
  contract_title: string;
  client_name: string;
  client_paypal_email: string;
  milestone_code: string;
  milestone_title: string;
  expected_amount: string;
  captured_amount: string | null;
  captured_currency: string | null;
  shortfall_amount: string;
  penalty_amount: string;
  total_remediation_amount: string;
  currency: string;
  due_date: string;
  captured_at: string | null;
  paypal_order_id: string | null;
  paypal_capture_id: string | null;
  webhook_verified: boolean;
  discrepancy_type: DiscrepancyType | null;
  discrepancy_status: DiscrepancyStatus;
  discrepancy_id: string | null;
  violated_clause_quote: string | null;
  ai_audit_summary: string | null;
  remediation_invoice: RemediationInvoiceRecord | null;
  leash_decision: LeashDecision | null;
  remediation_sent_by: RemediationSentBy | null;
}

export interface LedgerSummary {
  total_expected: string;
  total_captured: string;
  total_shortfall: string;
  total_penalties: string;
  total_remediation_invoiced: string;
}

export interface LedgerResponse {
  summary: LedgerSummary;
  rows: LedgerRow[];
}

export interface InvoiceLineItemSpec {
  name: string;
  description: string;
  quantity: string;
  unit_amount_value: string;
  currency_code: string;
}

export interface DiscrepancyEvaluationResult {
  discrepancy_type: DiscrepancyType;
  discrepancy_status: DiscrepancyStatus;
  is_compliant: boolean;
  expected_amount: string;
  captured_amount: string;
  captured_currency: string | null;
  shortfall_amount: string;
  penalty_amount: string;
  total_remediation_amount: string;
  violated_clause_quote: string;
  ai_audit_summary: string;
  paypal_invoice_detail_note: string;
  remediation_line_items: InvoiceLineItemSpec[];
}

export interface SimulatorPaymentRequest {
  milestone_id: string;
  simulated_amount: string;
  currency: string;
  simulated_payment_date: string;
  payer_email: string;
  idempotency_key?: string | null;
}

export interface SimulatorPaymentResponse {
  paypal_order_id: string;
  paypal_capture_id: string;
  evaluation: DiscrepancyEvaluationResult;
  leash: GuardResult | null;
  payroll_run_id: string | null;
}

export interface RemediationRequest {
  confirm_send: boolean;
  custom_note_override?: string | null;
  idempotency_key?: string | null;
}

export interface RemediationResponse {
  discrepancy_id: string;
  discrepancy_status: DiscrepancyStatus;
  remediation_invoice: RemediationInvoiceRecord;
}

export interface RemindResponse {
  discrepancy_id: string;
  remediation_invoice: RemediationInvoiceRecord;
}

export interface HealthResponse {
  status: string;
  paypal_sandbox_connected: boolean;
}

// --- Leash (Rev 2.1 §7.5 Interface 2 + A.2.2) ---

export type LeashDecision = "ALLOW" | "ASK" | "DENY";

export type GuardAction =
  | "SEND_REMEDIATION_INVOICE"
  | "PAY_TEAM_MEMBER"
  | "SEND_PAYROLL_BATCH";

export type PayrollMode = "MILESTONE" | "MONTHLY";

export type PayrollRunStatus =
  | "PREPARED"
  | "CHECKED"
  | "AWAITING_APPROVAL"
  | "SENT"
  | "PAID"
  | "PARTIAL"
  | "FAILED";

export type PayoutStatus = "PENDING" | "SUCCESS" | "UNCLAIMED" | "FAILED";

export type OwnerAction = "APPROVED" | "EXCLUDED";

export type TimesheetField = "member" | "work_date" | "hours" | "milestone_code" | "rate";

// Keys starting with "amount_" carry 2-dp Decimal strings (e.g. "500.00").
export type RuleConditionValue = string | number | boolean;

export type DecisionSubjectType = "discrepancy" | "payroll_line" | "payroll_run";

export type DecisionActor = "leash" | "owner";

export type RemediationSentBy = "owner" | "leash-auto";

export interface LeashRule {
  id: string; // 1-16 chars, e.g. "G-01"
  applies_to: GuardAction;
  when: Record<string, RuleConditionValue>;
  outcome: LeashDecision;
  source_text: string;
}

export interface LeashPolicy {
  version: number;
  rules: LeashRule[];
  payroll_margin_limit_pct: string; // Decimal serialized as "80.00"-style, default 80
  email_change_window_days: number; // default 7
}

export type ActiveLeashPolicy = LeashPolicy & { activated_at: string };

export interface GuardResult {
  decision: LeashDecision;
  rule_ids: string[];
  reason: string; // max 200 chars
  decision_id: string;
}

export interface ColumnMappingProposal {
  mapping: Record<string, TimesheetField>;
  confidence: Record<string, number>;
}

export interface PayrollLineOut {
  line_id: string;
  member_name: string;
  payout_email: string;
  hours: string; // Decimal serialized as "12.00"
  rate: string; // Decimal serialized as "25.00"
  gross: string; // Decimal serialized as "300.00"
  decision: LeashDecision;
  flags: string[];
  reason: string | null;
  owner_action: OwnerAction | null;
  payout_status: PayoutStatus | null;
}

export interface PayrollRunOut {
  run_id: string;
  mode: PayrollMode;
  milestone_id: string | null;
  period: string | null; // "YYYY-MM"
  status: PayrollRunStatus;
  total: string; // Decimal serialized as "720.00"
  cash_in: string;
  cash_used_pct: string;
  run_decision: LeashDecision;
  run_flags: string[];
  lines: PayrollLineOut[];
  sender_batch_id: string | null;
}

export interface PolicyCompileRequest {
  text: string; // 1-4000 chars
}

export interface PolicyCompileResponse {
  draft_version: number;
  status: "DRAFT";
  policy: LeashPolicy;
  cards: string[];
  test_cases: string[];
}

export interface PolicyActivateRequest {
  version: number;
}

export interface PolicyActivateResponse {
  version: number;
  active: boolean;
  activated_at: string;
}

export interface DecisionLogEntry {
  decision_id: string;
  created_at: string;
  policy_version: number;
  action_type: GuardAction;
  subject_type: DecisionSubjectType;
  subject_id: string;
  decision: LeashDecision;
  rule_ids: string[];
  amount: string | null;
  reason: string;
  actor: DecisionActor;
}

export interface DecisionLogResponse {
  items: DecisionLogEntry[];
  next_cursor: string | null;
}

export interface TeamMemberCreate {
  name: string;
  payout_email: string;
  hourly_rate: string; // Decimal serialized as "25.00", > 0
}

export interface TeamMemberUpdate {
  name?: string | null;
  payout_email?: string | null;
  hourly_rate?: string | null;
  active?: boolean | null;
}

export interface TeamMemberOut {
  member_id: string;
  name: string;
  payout_email: string;
  hourly_rate: string;
  email_changed_at: string | null;
  first_paid_at: string | null;
  active: boolean;
}

export interface TimesheetMappingRequired {
  upload_id: string;
  status: "MAPPING_REQUIRED";
  row_count: number;
  headers: string[];
  proposal: ColumnMappingProposal;
}

export interface TimesheetMappingConfirm {
  mapping: Record<string, TimesheetField>; // each target field mapped from at most one column
}

export interface TimesheetParsed {
  upload_id: string;
  status: "PARSED";
  entries: number;
  unknown_members: string[];
  milestones: string[];
  warnings: string[];
}

// Discriminated by `status`.
export type TimesheetUploadResponse = TimesheetMappingRequired | TimesheetParsed;

export interface PayrollRunCreate {
  mode: PayrollMode;
  milestone_id?: string | null; // required for MILESTONE, forbidden for MONTHLY
  period?: string | null; // "YYYY-MM"; required for MONTHLY, forbidden for MILESTONE
}

export interface PayrollLineUpdate {
  owner_action: OwnerAction;
  note?: string | null; // max 200 chars
}

export interface PayrollSendRequest {
  confirm: boolean;
  approve_run_flags?: string[];
  idempotency_key: string; // required: this endpoint moves money
}

export interface PayrollSendResponse {
  run_id: string;
  status: "SENT";
  sender_batch_id: string;
  paypal_batch_id: string;
  batch_status: string;
  total: string;
  lines_sent: number;
  lines_excluded: number;
}

export interface MarginRow {
  milestone_id: string;
  milestone_code: string;
  cash_in: string;
  invoiced_open: string;
  paid_to_team: string;
  pending_to_team: string;
  margin: string;
  margin_pct: string;
  payroll_status: PayrollRunStatus | null;
}

export interface MarginSummary {
  cash_in: string;
  paid_to_team: string;
  margin: string;
}

export interface MarginResponse {
  rows: MarginRow[];
  summary: MarginSummary;
}

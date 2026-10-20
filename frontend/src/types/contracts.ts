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

export type DiscrepancyStatus = "NONE" | "PENDING_APPROVAL" | "REMEDIATED";

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

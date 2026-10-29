import type { ContractActivateRequest, PersistedContractExtraction, PersistedMilestoneSpec } from "../../types/contracts";
import { isMoney, normaliseMoney } from "../../lib/money";

export type FieldErrors = Record<string, string>;

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export function validateTerms(t: PersistedContractExtraction): FieldErrors {
  const e: FieldErrors = {};
  if (!t.contract_title.trim()) e.contract_title = "Add a contract title.";
  if (!t.client_name.trim()) e.client_name = "Add the client's name.";
  if (!EMAIL_RE.test(t.client_paypal_email.trim())) e.client_paypal_email = "Enter the client's PayPal email.";
  if (!/^[A-Z]{3}$/.test(t.currency.trim())) e.currency = "Use a 3-letter code like USD.";
  if (!/^\d{1,2}(\.\d{1,2})?$/.test(t.late_fee_percentage.trim())) e.late_fee_percentage = "Enter a percentage like 5.00.";
  if (!Number.isInteger(t.grace_period_days) || t.grace_period_days < 0 || t.grace_period_days > 365)
    e.grace_period_days = "Whole days, 0 to 365.";
  if (t.milestones.length === 0) e.milestones = "Add at least one milestone.";
  const codes = new Set<string>();
  t.milestones.forEach((m, i) => {
    if (!m.milestone_code.trim()) e[`m${i}.milestone_code`] = "Code needed.";
    else if (codes.has(m.milestone_code.trim())) e[`m${i}.milestone_code`] = "Codes must be unique.";
    codes.add(m.milestone_code.trim());
    if (!m.title.trim()) e[`m${i}.title`] = "Title needed.";
    if (!isMoney(m.expected_amount) || Number(m.expected_amount) <= 0) e[`m${i}.expected_amount`] = "Amount like 1000.00.";
    if (!DATE_RE.test(m.due_date)) e[`m${i}.due_date`] = "Pick a due date.";
  });
  return e;
}

/** Shape the reviewed terms for POST /contracts/{id}/activate (money normalised to 2dp). */
export function toActivateRequest(t: PersistedContractExtraction): ContractActivateRequest {
  return {
    contract_title: t.contract_title.trim(),
    client_name: t.client_name.trim(),
    client_paypal_email: t.client_paypal_email.trim(),
    currency: t.currency.trim().toUpperCase(),
    late_fee_percentage: normaliseMoney(t.late_fee_percentage),
    grace_period_days: t.grace_period_days,
    allow_partial_payments: t.allow_partial_payments,
    penalty_clause_quote: t.penalty_clause_quote.trim(),
    milestones: t.milestones.map((m) => ({
      ...m,
      milestone_code: m.milestone_code.trim(),
      title: m.title.trim(),
      expected_amount: normaliseMoney(m.expected_amount),
      source_clause_quote: m.source_clause_quote.trim(),
    })),
  };
}

export function emptyMilestone(index: number): PersistedMilestoneSpec {
  return {
    milestone_id: "",
    milestone_code: `MS-${String(index + 1).padStart(2, "0")}`,
    title: "",
    expected_amount: "",
    due_date: "",
    source_clause_quote: "",
  };
}

/** "Section 4.2: ..." -> "Section 4.2" so the text pane can find the paragraph. */
export function sectionLabel(quote: string | null | undefined): string | null {
  if (!quote) return null;
  const m = /Section\s+\d+(?:\.\d+)*/i.exec(quote);
  return m ? m[0].replace(/\s+/g, " ") : null;
}

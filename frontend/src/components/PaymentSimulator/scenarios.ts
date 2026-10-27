import type { LedgerRow, SimulatorPaymentRequest } from "../../types/contracts";
import { addDays } from "../../lib/dates";
import { centsToString, toCents } from "../../lib/money";

export type ScenarioId = "A" | "B" | "C";

export interface CustomInputs {
  amount: string;
  date: string;
  currency: string;
}

export const SCENARIOS: { id: ScenarioId; title: string; describe: (r: LedgerRow | null) => string }[] = [
  {
    id: "A",
    title: "Pay in full, on time",
    describe: (r) => (r ? `${r.expected_amount} ${r.currency} on ${r.due_date}` : "Full amount on the due date"),
  },
  {
    id: "B",
    title: "Underpay, five days late",
    describe: (r) => (r ? `${underpay(r)} ${r.currency} on ${addDays(r.due_date, 5)}` : "85% of the amount, 5 days after the due date"),
  },
  { id: "C", title: "Custom", describe: () => "Pick the amount, date and currency" },
];

/** Scenario B: the $1,000 milestone pays $850, i.e. 85% (rounded to whole cents). */
export function underpay(r: LedgerRow): string {
  return centsToString(Math.round((toCents(r.expected_amount) * 85) / 100));
}

export function buildRequest(
  row: LedgerRow,
  scenario: ScenarioId,
  custom: CustomInputs,
  payerEmail: string,
  idempotencyKey: string,
): SimulatorPaymentRequest {
  const base = { milestone_id: row.milestone_id, payer_email: payerEmail.trim() || row.client_paypal_email, idempotency_key: idempotencyKey };
  if (scenario === "A") {
    return { ...base, simulated_amount: centsToString(toCents(row.expected_amount)), currency: row.currency, simulated_payment_date: row.due_date };
  }
  if (scenario === "B") {
    return { ...base, simulated_amount: underpay(row), currency: row.currency, simulated_payment_date: addDays(row.due_date, 5) };
  }
  return {
    ...base,
    simulated_amount: centsToString(toCents(custom.amount)),
    currency: custom.currency.trim().toUpperCase() || row.currency,
    simulated_payment_date: custom.date || row.due_date,
  };
}

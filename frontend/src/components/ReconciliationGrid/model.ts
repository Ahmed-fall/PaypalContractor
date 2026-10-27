import type { LedgerRow, MarginPayrollStatus, MarginResponse, MarginRow } from "../../types/contracts";
import { centsToString, toCents } from "../../lib/money";

/** One ledger row joined with its money-out view (plan H4, REQ-LSH-14). */
export interface GridRow extends LedgerRow {
  __key: string;
  __detailFor?: string; // set on the full-width detail row under an expanded milestone
  cash_in: string;
  invoiced_open: string;
  paid_to_team: string;
  pending_to_team: string;
  margin: string;
  margin_pct: string;
  payroll_status: MarginPayrollStatus;
}

export interface TotalsRow {
  __key: "__totals";
  __totals: true;
  milestone_code: string;
  currency: string;
  expected_amount: string;
  captured_amount: string;
  shortfall_amount: string;
  penalty_amount: string;
  total_remediation_amount: string;
  paid_to_team: string;
  margin: string;
  rows: number;
}

/**
 * Null-safe join. A milestone with no payroll run, a row the margin endpoint did not
 * return, or Leash switched off all coalesce to "0.00" with status NONE, so nothing
 * downstream can produce NaN.
 */
export function joinMargin(rows: LedgerRow[], margin: MarginResponse | null): GridRow[] {
  const byId = new Map<string, MarginRow>();
  for (const m of margin?.rows ?? []) byId.set(m.milestone_id, m);
  return rows.map((r) => {
    const m = byId.get(r.milestone_id);
    return {
      ...r,
      __key: r.milestone_id,
      cash_in: centsToString(toCents(m?.cash_in)),
      invoiced_open: centsToString(toCents(m?.invoiced_open)),
      paid_to_team: centsToString(toCents(m?.paid_to_team)),
      pending_to_team: centsToString(toCents(m?.pending_to_team)),
      margin: centsToString(toCents(m?.margin)),
      margin_pct: m?.margin_pct ?? "0.00",
      payroll_status: m?.payroll_status ?? "NONE",
    };
  });
}

/** Pinned bottom totals, summed in integer cents (plan R4). */
export function computeTotals(rows: GridRow[]): TotalsRow {
  const sum = (pick: (r: GridRow) => string | null | undefined) => centsToString(rows.reduce((acc, r) => acc + toCents(pick(r)), 0));
  const currencies = new Set(rows.map((r) => r.currency));
  return {
    __key: "__totals",
    __totals: true,
    milestone_code: `Totals, ${rows.length} milestone${rows.length === 1 ? "" : "s"}`,
    currency: currencies.size === 1 ? [...currencies][0] : "USD",
    expected_amount: sum((r) => r.expected_amount),
    // Foreign-currency captures are excluded, matching the API's total_captured.
    captured_amount: sum((r) => (!r.captured_currency || r.captured_currency === r.currency ? r.captured_amount : "0.00")),
    shortfall_amount: sum((r) => r.shortfall_amount),
    penalty_amount: sum((r) => r.penalty_amount),
    total_remediation_amount: sum((r) => (r.discrepancy_type === "COMPLIANT" ? "0.00" : r.total_remediation_amount)),
    paid_to_team: sum((r) => r.paid_to_team),
    margin: sum((r) => r.margin),
    rows: rows.length,
  };
}

export type StatusFilter = "all" | "action" | "owed" | "sent" | "compliant" | "unpaid";

export function matchesStatus(r: GridRow, f: StatusFilter): boolean {
  switch (f) {
    case "all":
      return true;
    case "action":
      return r.discrepancy_status === "PENDING_APPROVAL" || r.discrepancy_status === "BLOCKED";
    case "owed":
      return toCents(r.total_remediation_amount) > 0 && r.discrepancy_status !== "REMEDIATED";
    case "sent":
      return r.discrepancy_status === "REMEDIATED";
    case "compliant":
      return r.discrepancy_type === "COMPLIANT";
    case "unpaid":
      return !r.captured_amount;
  }
}

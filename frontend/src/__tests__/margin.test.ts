import { describe, expect, it } from "vitest";
import { computeTotals, joinMargin } from "../components/ReconciliationGrid/model";
import type { LedgerRow, MarginResponse } from "../types/contracts";

function row(id: string, patch: Partial<LedgerRow> = {}): LedgerRow {
  return {
    milestone_id: id,
    contract_id: "cnt_1",
    contract_title: "Agreement",
    client_name: "Acme Corp",
    client_paypal_email: "sb-buyer@personal.example.com",
    milestone_code: id.toUpperCase(),
    milestone_title: "Phase",
    expected_amount: "1000.00",
    captured_amount: "850.00",
    captured_currency: "USD",
    shortfall_amount: "150.00",
    penalty_amount: "50.00",
    total_remediation_amount: "200.00",
    currency: "USD",
    due_date: "2026-10-15",
    captured_at: "2026-10-20T14:22:10Z",
    paypal_order_id: "O",
    paypal_capture_id: "C",
    webhook_verified: true,
    discrepancy_type: "UNDERPAID_AND_LATE",
    discrepancy_status: "REMEDIATED",
    discrepancy_id: "dsc_1",
    violated_clause_quote: "Section 4.2",
    ai_audit_summary: "Short",
    remediation_invoice: null,
    leash_decision: "ALLOW",
    remediation_sent_by: "leash-auto",
    ...patch,
  };
}

const margin: MarginResponse = {
  rows: [
    {
      milestone_id: "ms_01",
      milestone_code: "MS-01",
      cash_in: "850.00",
      invoiced_open: "200.00",
      paid_to_team: "420.00",
      pending_to_team: "0.00",
      margin: "430.00",
      margin_pct: "50.59",
      payroll_status: "PAID",
    },
  ],
  summary: { cash_in: "850.00", paid_to_team: "420.00", margin: "430.00" },
};

describe("ledger margin join (REQ-LSH-14, TST-11)", () => {
  it("shows MS-01 received, invoiced, paid to team and margin", () => {
    const [ms01] = joinMargin([row("ms_01")], margin);
    expect(ms01).toMatchObject({ cash_in: "850.00", invoiced_open: "200.00", paid_to_team: "420.00", margin: "430.00", payroll_status: "PAID" });
  });

  it("coalesces a milestone with no payroll run to 0.00 / NONE", () => {
    const rows = joinMargin([row("ms_01"), row("ms_02", { captured_amount: null })], margin);
    expect(rows[1]).toMatchObject({ paid_to_team: "0.00", margin: "0.00", pending_to_team: "0.00", payroll_status: "NONE" });
  });

  it("never renders NaN in pinned totals, even with Leash off (margin null)", () => {
    const rows = joinMargin([row("ms_01"), row("ms_02", { captured_amount: null, shortfall_amount: "", penalty_amount: "" })], null);
    const totals = computeTotals(rows);
    expect(JSON.stringify(totals)).not.toContain("NaN");
    expect(totals.paid_to_team).toBe("0.00");
    expect(totals.margin).toBe("0.00");
    expect(totals.captured_amount).toBe("850.00");
  });

  it("excludes foreign-currency captures from captured totals", () => {
    const rows = joinMargin([row("ms_01"), row("ms_02", { captured_amount: "2500.00", captured_currency: "EUR" })], margin);
    expect(computeTotals(rows).captured_amount).toBe("850.00");
  });

  it("sums totals in cents", () => {
    const rows = joinMargin(
      [row("a", { expected_amount: "0.10" }), row("b", { expected_amount: "0.20" }), row("c", { expected_amount: "849.70" })],
      null,
    );
    expect(computeTotals(rows).expected_amount).toBe("850.00");
  });
});

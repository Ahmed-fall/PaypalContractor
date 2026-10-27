import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RemediationModal } from "../components/RemediationModal";
import { setTransport, type TransportRequest } from "../api/client";
import { setDemoKey } from "../api/demoKey";
import type { LedgerRow } from "../types/contracts";

vi.mock("../lib/qr", () => ({ qrDataUrl: vi.fn(async () => "data:image/png;base64,LOCALQR") }));

const pending: LedgerRow = {
  milestone_id: "ms_01",
  contract_id: "cnt_01J9Z8A",
  contract_title: "Master Software Development Agreement - Acme Corp",
  client_name: "Acme Corp",
  client_paypal_email: "sb-buyer@personal.example.com",
  milestone_code: "MS-01",
  milestone_title: "Phase 1: Backend Architecture Delivery",
  expected_amount: "1000.00",
  captured_amount: "850.00",
  captured_currency: "USD",
  shortfall_amount: "150.00",
  penalty_amount: "50.00",
  total_remediation_amount: "200.00",
  currency: "USD",
  due_date: "2026-10-15",
  captured_at: "2026-10-20T14:22:10Z",
  paypal_order_id: "5O190127TN364715T",
  paypal_capture_id: "2GG279541U471931P",
  webhook_verified: true,
  discrepancy_type: "UNDERPAID_AND_LATE",
  discrepancy_status: "PENDING_APPROVAL",
  discrepancy_id: "dsc_01J9Z99",
  violated_clause_quote: "Section 4.2: Any payment received below the milestone amount ...",
  ai_audit_summary: "Client paid $850.00 against $1,000.00 expected.",
  remediation_invoice: null,
  leash_decision: "ASK",
  remediation_sent_by: null,
};

const invoice = {
  paypal_invoice_id: "INV2-X4Y5-Z6A7-B8C9-D0E1",
  paypal_invoice_number: "FD-2026-0001",
  status: "SENT",
  total_amount: "200.00",
  currency: "USD",
  recipient_email: "sb-buyer@personal.example.com",
  payer_view_url: "https://www.sandbox.paypal.com/invoice/p/#X4Y5Z6A7B8C9D0E1",
  qr_code_base64: "iVBORw0KGgoAAAANSUhEUgAA",
  sent_at: "2026-10-20T14:23:05Z",
};

describe("RemediationModal (TST-11)", () => {
  let calls: TransportRequest[];
  let release: () => void;

  beforeEach(() => {
    calls = [];
    setDemoKey("k");
    setTransport(async (req) => {
      if (req.path === "/leash/decisions") {
        return {
          status: 200,
          body: {
            items: [
              {
                decision_id: "lsd_1",
                created_at: "2026-10-20T14:22:12Z",
                policy_version: 2,
                action_type: "SEND_REMEDIATION_INVOICE",
                subject_type: "discrepancy",
                subject_id: "dsc_01J9Z99",
                decision: "ASK",
                rule_ids: ["G-02"],
                amount: "200.00",
                reason: "Over your auto-send limit, so Leash is asking you first.",
                actor: "leash",
              },
            ],
            next_cursor: null,
          },
        };
      }
      calls.push(req);
      // Hold the response so the test can click again while the request is in flight.
      await new Promise<void>((r) => (release = r));
      return { status: 200, body: { discrepancy_id: "dsc_01J9Z99", discrepancy_status: "REMEDIATED", remediation_invoice: invoice } };
    });
  });

  it("shows Leash's Ask reason and the math", async () => {
    render(<RemediationModal row={pending} open onClose={() => {}} onSent={() => {}} onStale={() => {}} />);
    expect(await screen.findByText(/Leash is asking you first/)).toBeInTheDocument();
    expect(screen.getByText("Correction invoice")).toBeInTheDocument();
    expect(screen.getAllByText("$200.00").length).toBeGreaterThan(0);
  });

  it("locks Approve & Send on the first click: a double click sends one request", async () => {
    const onSent = vi.fn();
    const user = userEvent.setup();
    render(<RemediationModal row={pending} open onClose={() => {}} onSent={onSent} onStale={() => {}} />);
    const btn = await screen.findByTestId("approve-send");
    await user.dblClick(btn);
    await user.click(btn);
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe("/discrepancies/dsc_01J9Z99/remediate");
    expect(calls[0].body).toMatchObject({ confirm_send: true, idempotency_key: "rem_dsc_01J9Z99" });
    expect(calls[0].headers["X-Demo-Key"]).toBe("k");
    expect(screen.getByTestId("approve-send")).toBeDisabled();

    await act(async () => release());
    expect(await screen.findByTestId("invoice-id")).toHaveTextContent("INV2-X4Y5-Z6A7-B8C9-D0E1");
    expect(onSent).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("approve-send")).not.toBeInTheDocument();
    // PayPal's QR is unusable here, so the browser draws one from payer_view_url (Fallback B).
    expect(await screen.findByTestId("qr-local")).toHaveAttribute("src", "data:image/png;base64,LOCALQR");
  });

  it("shows the Leash auto-send banner and no Approve button for an auto-sent invoice", async () => {
    const sent: LedgerRow = { ...pending, discrepancy_status: "REMEDIATED", remediation_sent_by: "leash-auto", leash_decision: "ALLOW", remediation_invoice: invoice };
    render(<RemediationModal row={sent} open onClose={() => {}} onSent={() => {}} onStale={() => {}} />);
    expect(await screen.findByTestId("leash-auto-banner")).toHaveTextContent(/Sent automatically by Leash/);
    expect(screen.queryByTestId("approve-send")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("invoice-id")).toBeInTheDocument());
  });
});

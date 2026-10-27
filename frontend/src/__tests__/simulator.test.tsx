import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api, setTransport } from "../api/client";
import { setDemoKey } from "../api/demoKey";
import { mockTransport, resetMockState, setMockLatency } from "../api/mocks";
import { PaymentSimulator } from "../components/PaymentSimulator";
import { on } from "../lib/events";

vi.mock("../lib/qr", () => ({ qrDataUrl: vi.fn(async () => "data:image/png;base64,LOCALQR") }));

describe("Payment simulator against the fixture-backed mock API (TST-11)", () => {
  beforeEach(() => {
    setMockLatency(0);
    resetMockState();
    setTransport(mockTransport);
    setDemoKey("demo");
  });

  it("$850 five days late on Acme MS-01 is UNDERPAID_AND_LATE, Leash ALLOWs and the invoice is auto-sent", async () => {
    const { rows } = await api.ledger();
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <PaymentSimulator rows={rows} initialMilestoneId="ms_01" />
      </MemoryRouter>,
    );
    await user.click(screen.getByLabelText(/B · Underpay, five days late/));
    await user.click(screen.getByTestId("sim-run"));
    const result = await screen.findByTestId("sim-result");
    expect(within(result).getByText("Underpaid + late")).toBeInTheDocument();
    expect(within(result).getByText("$200.00")).toBeInTheDocument();
    expect(within(screen.getByTestId("sim-leash")).getByText("Leash: ALLOW")).toBeInTheDocument();
    expect(await screen.findByTestId("sim-invoice-id")).toHaveTextContent(/^INV2-/);

    const after = await api.ledger();
    const ms01 = after.rows.find((r) => r.milestone_id === "ms_01")!;
    expect(ms01).toMatchObject({
      captured_amount: "850.00",
      shortfall_amount: "150.00",
      penalty_amount: "50.00",
      total_remediation_amount: "200.00",
      discrepancy_type: "UNDERPAID_AND_LATE",
      discrepancy_status: "REMEDIATED",
      remediation_sent_by: "leash-auto",
    });
    const margin = await api.margin();
    expect(margin.rows.find((m) => m.milestone_id === "ms_01")).toMatchObject({ cash_in: "850.00", paid_to_team: "420.00", margin: "430.00" });
  });

  it("asks for the owner key when locked (401 opens the Unlock dialog)", async () => {
    setDemoKey(null);
    const unlock = vi.fn();
    const off = on("unlock-required", unlock);
    await expect(api.simulatePay({ milestone_id: "ms_01", simulated_amount: "850.00", currency: "USD", simulated_payment_date: "2026-10-20", payer_email: "x@y.z" })).rejects.toMatchObject({
      status: 401,
      code: "DEMO_KEY_REQUIRED",
    });
    expect(unlock).toHaveBeenCalledTimes(1);
    off();
  });

  it("replays the same idempotency key instead of capturing twice", async () => {
    const body = { milestone_id: "ms_01", simulated_amount: "850.00", currency: "USD", simulated_payment_date: "2026-10-20", payer_email: "x@y.z", idempotency_key: "sim_req_001" };
    const a = await api.simulatePay(body);
    const b = await api.simulatePay(body);
    expect(b.paypal_capture_id).toBe(a.paypal_capture_id);
  });

  it("rejects payments against a contract that is not active", async () => {
    const ex = await api.extractContract({ raw_text: "Acme Corp agreement" });
    expect(ex.status).toBe("DRAFT");
    await expect(
      api.simulatePay({ milestone_id: "ms_01", simulated_amount: "1000.00", currency: "USD", simulated_payment_date: "2026-10-15", payer_email: "x@y.z" }),
    ).rejects.toMatchObject({ status: 409, code: "CONTRACT_NOT_ACTIVE" });
  });
});

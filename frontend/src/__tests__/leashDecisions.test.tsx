import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { setTransport, type Transport, type TransportRequest } from "../api/client";
import { setDemoKey } from "../api/demoKey";
import { mockTransport, resetMockState, setMockLatency } from "../api/mocks";
import type { DecisionLogEntry, DecisionLogResponse } from "../types/contracts";
import App from "../App";
import DecisionsPage from "../components/Leash/DecisionsPage";

const router = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

const entry = (n: number, over: Partial<DecisionLogEntry> = {}): DecisionLogEntry => ({
  decision_id: `lsd_${String(n).padStart(4, "0")}`,
  created_at: "2026-10-20T14:22:12Z",
  policy_version: 2,
  action_type: "SEND_REMEDIATION_INVOICE",
  subject_type: "discrepancy",
  subject_id: `dsc_${n}`,
  decision: "ALLOW",
  rule_ids: ["G-01"],
  amount: "200.00",
  reason: `Reason number ${n}`,
  actor: "leash",
  ...over,
});

const json = (body: DecisionLogResponse) => ({ status: 200, body });

function renderPage() {
  return render(
    <MemoryRouter future={router}>
      <DecisionsPage />
    </MemoryRouter>,
  );
}

describe("Leash decision log (WP-5.5)", () => {
  beforeEach(() => {
    setMockLatency(0);
    resetMockState();
    setTransport(mockTransport);
    setDemoKey("demo");
  });

  it("decisions render with plain labels", async () => {
    const page: DecisionLogResponse = {
      items: [
        entry(1),
        entry(2, { action_type: "SEND_PAYROLL_BATCH", subject_type: "payroll_run", decision: "ASK", rule_ids: ["PR-01"], amount: "1250.00", actor: "owner" }),
        entry(3, { action_type: "PAY_TEAM_MEMBER", subject_type: "payroll_line", decision: "DENY", rule_ids: ["PR-06"], amount: null }),
      ],
      next_cursor: null,
    };
    setTransport(async () => json(page));
    renderPage();
    const grid = await screen.findByRole("region", { name: "Leash decisions" });
    expect(await within(grid).findByText("Correction invoice")).toBeInTheDocument();
    expect(within(grid).getByText("Send payroll batch")).toBeInTheDocument();
    expect(within(grid).getByText("Pay team member")).toBeInTheDocument();
    expect(within(grid).getByText("Allow")).toBeInTheDocument();
    expect(within(grid).getByText("Ask")).toBeInTheDocument();
    expect(within(grid).getByText("Deny")).toBeInTheDocument();
    expect(within(grid).getByText("Small invoice, clause quoted")).toBeInTheDocument();
    expect(within(grid).getByText("Payout email changed")).toBeInTheDocument();
    expect(within(grid).getByText("$200.00")).toBeInTheDocument();
    expect(within(grid).getByText("$1,250.00")).toBeInTheDocument();
    expect(within(grid).getAllByText("Leash")).toHaveLength(2);
    expect(within(grid).getByText("You")).toBeInTheDocument();
    expect(within(grid).getByText("Reason number 1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("load more appends using next_cursor", async () => {
    const calls: TransportRequest[] = [];
    const t: Transport = async (req) => {
      calls.push(req);
      return req.query.cursor
        ? json({ items: [entry(2)], next_cursor: null })
        : json({ items: [entry(1)], next_cursor: "2026-10-20T14:22:12+00:00|lsd_0001" });
    };
    setTransport(t);
    const user = userEvent.setup();
    renderPage();
    const grid = await screen.findByRole("region", { name: "Leash decisions" });
    expect(await within(grid).findByText("Reason number 1")).toBeInTheDocument();
    expect(calls[0].query.limit).toBe("50");
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await within(grid).findByText("Reason number 2")).toBeInTheDocument();
    expect(within(grid).getByText("Reason number 1")).toBeInTheDocument();
    expect(calls[1].query.cursor).toBe("2026-10-20T14:22:12+00:00|lsd_0001");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument());
  });

  it("filters by subject type with the backend param", async () => {
    const calls: TransportRequest[] = [];
    setTransport(async (req) => {
      calls.push(req);
      return json({ items: [entry(1)], next_cursor: null });
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("region", { name: "Leash decisions" });
    await user.selectOptions(screen.getByLabelText("Subject type"), "payroll_run");
    await waitFor(() => expect(calls[calls.length - 1]?.query.subject_type).toBe("payroll_run"));
  });

  it("a failed load shows an inline error and Retry recovers", async () => {
    let fail = true;
    setTransport(async () => {
      if (fail) return { status: 500, body: { error: { code: "INTERNAL", message: "boom", details: {}, request_id: "r1" } } };
      return json({ items: [entry(1)], next_cursor: null });
    });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Reason number 1")).toBeInTheDocument();
  });
});

describe("Leash routes (WP-5.5)", () => {
  beforeEach(() => {
    setMockLatency(0);
    resetMockState();
    setTransport(mockTransport);
    setDemoKey("demo");
  });

  it.each([
    ["/leash/rules", /^Rules$/],
    ["/leash/team", /^Team$/],
    ["/leash/payroll", /^Payroll$/],
    ["/leash/payroll/run_demo", /^Payroll run/],
    ["/leash/decisions", /^Decisions$/],
  ])("app routes render the Leash pages: %s", async (path, heading) => {
    render(
      <MemoryRouter initialEntries={[path]} future={router}>
        <App />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect(screen.queryByText(/built by the Leash owner/i)).not.toBeInTheDocument();
  });
});

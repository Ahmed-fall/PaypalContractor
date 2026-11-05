import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { on } from "../lib/events";
import { setTransport, type Transport, type TransportRequest, type TransportResponse } from "../api/client";
import { setDemoKey } from "../api/demoKey";
import { mockTransport, resetMockState, setMockLatency } from "../api/mocks";
import PayrollRunPage from "../components/Leash/PayrollRunPage";
import { canApprove, isFinished, openAsks, parseOpenAsks, payBlockers, runTotalsCents, sendKey } from "../components/Leash/run";
import type { PayrollLineOut, PayrollRunOut, PayrollSendRequest } from "../types/contracts";
import awaiting from "../../../shared/fixtures/leash/payroll_run_awaiting_approval.json";
import afterExclude from "../../../shared/fixtures/leash/payroll_run_after_exclude.json";

const router = { v7_startTransition: true, v7_relativeSplatPath: true } as const;
const RUN = "prn_0001";
const runPath = `/leash/payroll/runs/${RUN}`;
const sendPath = `${runPath}/send`;

const clone = <T,>(v: unknown): T => structuredClone(v) as T;
const awaitingRun = () => clone<PayrollRunOut>(awaiting);
const checkedRun = () => clone<PayrollRunOut>(afterExclude);

function sentRun(): PayrollRunOut {
  const r = checkedRun();
  r.status = "SENT";
  r.sender_batch_id = "fd-ms01-prn_0001";
  for (const l of r.lines) if (l.owner_action !== "EXCLUDED") l.payout_status = "PENDING";
  return r;
}

function renderRun() {
  return render(
    <MemoryRouter future={router} initialEntries={[`/leash/payroll/${RUN}`]}>
      <Routes>
        <Route path="/leash/payroll/:runId" element={<PayrollRunPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const json = (status: number, body: unknown): TransportResponse => ({ status, body });
const apiErr = (status: number, code: string, message: string, details: Record<string, unknown> | null = null) =>
  json(status, { error: { code, message, details, request_id: "req_test" } });

/** Wrap the mock so a test can change one answer and see every call. */
function spyTransport(override?: (req: TransportRequest) => ReturnType<Transport> | TransportResponse | null) {
  const calls: TransportRequest[] = [];
  const t: Transport = async (req) => {
    calls.push(req);
    return (await override?.(req)) ?? mockTransport(req);
  };
  setTransport(t);
  return calls;
}

const runGets = (calls: TransportRequest[]) => calls.filter((c) => c.method === "GET" && c.path === runPath);
const sends = (calls: TransportRequest[]) => calls.filter((c) => c.method === "POST" && c.path === sendPath);

/** Advance fake timers and let React settle. */
async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function payTeam() {
  return screen.getByRole("button", { name: "Pay team" });
}

describe("Leash payroll run review (WP-5.5)", () => {
  beforeEach(() => {
    setMockLatency(0);
    resetMockState();
    setTransport(mockTransport);
    setDemoKey("demo");
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("demo flow: exclude Omar, pay the team once and watch the statuses land", async () => {
    const calls = spyTransport();
    const toasts: unknown[] = [];
    const off = on("api-error", (e) => toasts.push(e));
    const user = userEvent.setup();
    renderRun();

    expect(await screen.findByText("Payout email changed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve Omar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Exclude Omar" })).toBeInTheDocument();
    expect(screen.getByText("$720.00")).toBeInTheDocument();
    expect(payTeam()).toBeDisabled();
    expect(screen.getByText(/1 line still needs your decision/)).toBeInTheDocument();
    expect(screen.getByText(/Approve the 80% check above/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Approve: Over 80% of cash received" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Exclude Omar" }));
    expect(await screen.findByText("$420.00")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Over 80% of cash received/ })).not.toBeInTheDocument();
    expect(screen.queryByText("Over 80% of cash received")).not.toBeInTheDocument();
    expect(payTeam()).toBeEnabled();
    // Excluded Omar can come back: his line was checked, so Undo is offered.
    expect(screen.getByRole("button", { name: "Undo exclude Omar" })).toBeInTheDocument();

    await user.click(payTeam());
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("Send $420.00 to 2 people through PayPal Payouts? This can't be undone."),
    ).toBeInTheDocument();

    vi.useFakeTimers();
    fireEvent.click(within(dialog).getByRole("button", { name: "Send payment" }));
    await tick(0);
    expect(sends(calls)).toHaveLength(1);
    expect(screen.getByText("Sent to PayPal. Batch 5UXD2E8A7EBQJ")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Exclude/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pay team" })).not.toBeInTheDocument();

    await tick(3000);
    await tick(3000);
    expect(screen.getByText("Paid: 2 people, $420.00")).toBeInTheDocument();
    const sara = screen.getByRole("row", { name: /Sara/ });
    const youssef = screen.getByRole("row", { name: /Youssef/ });
    expect(within(sara).getByText("Paid")).toBeInTheDocument();
    expect(within(youssef).getByText("Paid")).toBeInTheDocument();

    const settled = runGets(calls).length;
    await tick(10000);
    expect(runGets(calls)).toHaveLength(settled); // stops on PAID
    expect(sends(calls)).toHaveLength(1);
    expect(toasts).toHaveLength(0);
    off();
  });

  it("test_pay_team_sends_once", async () => {
    let release: (() => void) | null = null;
    let batchOut = false;
    const calls = spyTransport((req) => {
      if (req.method === "GET" && req.path === runPath) return json(200, batchOut ? sentRun() : checkedRun());
      if (req.method === "POST" && req.path === sendPath) {
        return new Promise<TransportResponse>((resolve) => {
          release = () => {
            batchOut = true;
            resolve(
              json(202, {
                run_id: RUN,
                status: "SENT",
                sender_batch_id: "fd-ms01-prn_0001",
                paypal_batch_id: "5UXD2E8A7EBQJ",
                batch_status: "PENDING",
                total: "420.00",
                lines_sent: 2,
                lines_excluded: 1,
              }),
            );
          };
        });
      }
      return null;
    });
    const user = userEvent.setup();
    renderRun();
    await waitFor(() => expect(payTeam()).toBeEnabled());

    await user.click(payTeam());
    const confirm = within(await screen.findByRole("dialog")).getByRole("button", { name: "Send payment" });
    await user.dblClick(confirm);
    await user.click(confirm); // and once more while the first is still in flight
    expect(sends(calls)).toHaveLength(1);
    expect(confirm).toBeDisabled();
    expect(payTeam()).toBeDisabled();

    await act(async () => release?.());
    expect(await screen.findByText("Sent to PayPal. Batch 5UXD2E8A7EBQJ")).toBeInTheDocument();
    expect(sends(calls)).toHaveLength(1);
    const body = sends(calls)[0].body as PayrollSendRequest;
    expect(body).toEqual({ confirm: true, approve_run_flags: [], idempotency_key: sendKey(RUN) });
  });

  it("test_pay_team_sends_once: a retry after a lost answer reuses the same idempotency key", async () => {
    let first = true;
    const calls = spyTransport((req) => {
      if (req.method === "GET" && req.path === runPath) return json(200, checkedRun());
      if (req.method === "POST" && req.path === sendPath && first) {
        first = false;
        return json(0, null); // network dropped: the server may or may not have it
      }
      return null;
    });
    const toasts: unknown[] = [];
    const off = on("api-error", (e) => toasts.push(e));
    const user = userEvent.setup();
    renderRun();
    await waitFor(() => expect(payTeam()).toBeEnabled());

    await user.click(payTeam());
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Send payment" }));
    expect(await screen.findByText(/Could not reach the server/)).toBeInTheDocument();

    await user.click(payTeam());
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Send payment" }));
    await waitFor(() => expect(sends(calls)).toHaveLength(2));
    const [a, b] = sends(calls).map((c) => (c.body as PayrollSendRequest).idempotency_key);
    expect(a).toBe(b);
    expect(a).toBe(sendKey(RUN));
    expect(toasts).toHaveLength(0);
    off();
  });

  it("test_unchecked_line_cannot_be_approved", async () => {
    const run = awaitingRun();
    run.lines[0].flags = ["DEFAULT"];
    run.lines[0].reason = "Leash couldn't check this line yet.";
    run.run_decision = "ALLOW";
    run.run_flags = [];
    spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, structuredClone(run)) : null));
    renderRun();

    expect(await screen.findByRole("button", { name: "Exclude Omar" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve Omar" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("row", { name: /Omar/ })).getByText("Not checked yet")).toBeInTheDocument();
    expect(payTeam()).toBeDisabled();
    expect(screen.getByText(/1 line still needs your decision/)).toBeInTheDocument();

    // and the pure rule behind it
    expect(canApprove(run.lines[0], "AWAITING_APPROVAL")).toBe(false);
    const deny: PayrollLineOut = { ...run.lines[1], decision: "DENY", flags: ["PR-07"] };
    expect(canApprove(deny, "AWAITING_APPROVAL")).toBe(false);
    expect(canApprove(awaitingRun().lines[0], "AWAITING_APPROVAL")).toBe(true);
    expect(canApprove(awaitingRun().lines[0], "SENT")).toBe(false);
  });

  it("test_run_totals_use_cents", () => {
    const run = checkedRun();
    run.lines[1].gross = "0.10";
    run.lines[2].gross = "0.20";
    const t = runTotalsCents(run);
    expect(t.payCents).toBe(30);
    expect(t.people).toBe(2);

    const many = checkedRun();
    many.lines = Array.from({ length: 10 }, (_, i) => ({ ...many.lines[1], line_id: `prl_${i}`, gross: "0.10" }));
    expect(runTotalsCents(many).payCents).toBe(100); // 0.1 * 10 in floats is 0.9999999999999999

    const paid = sentRun();
    paid.lines[1].payout_status = "SUCCESS";
    paid.lines[2].payout_status = "FAILED";
    expect(runTotalsCents(paid)).toMatchObject({ paidCents: 22000, paidPeople: 1, sentPeople: 2 });
  });

  it("test_polling_stops_on_unmount", async () => {
    vi.useFakeTimers();
    const calls = spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, sentRun()) : null));
    const view = renderRun();
    await tick(0);
    expect(runGets(calls)).toHaveLength(1);
    expect(screen.getByText(/Sent to PayPal/)).toBeInTheDocument();

    await tick(2999);
    expect(runGets(calls)).toHaveLength(1);
    await tick(1);
    expect(runGets(calls)).toHaveLength(2);
    await tick(3000);
    expect(runGets(calls)).toHaveLength(3);

    view.unmount();
    const before = calls.length;
    await tick(30000);
    expect(calls.length).toBe(before);
  });

  it("does not poll a run that is waiting for approval, and stops once it is finished", async () => {
    vi.useFakeTimers();
    let answer: PayrollRunOut = awaitingRun();
    const calls = spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, structuredClone(answer)) : null));
    const view = renderRun();
    await tick(0);
    await tick(9000);
    expect(runGets(calls)).toHaveLength(1);
    view.unmount();

    answer = sentRun();
    calls.length = 0;
    renderRun();
    await tick(0);
    answer = sentRun();
    answer.status = "PARTIAL";
    answer.lines[1].payout_status = "SUCCESS";
    answer.lines[2].payout_status = "UNCLAIMED";
    await tick(3000);
    expect(screen.getByText(/^Partly paid:/)).toBeInTheDocument();
    const settled = runGets(calls).length;
    await tick(15000);
    expect(runGets(calls)).toHaveLength(settled);
    expect(isFinished("PAID") && isFinished("PARTIAL") && isFinished("FAILED")).toBe(true);
    expect(isFinished("SENT")).toBe(false);
  });

  it("409 open asks are highlighted", async () => {
    const calls = spyTransport((req) => {
      if (req.method === "GET" && req.path === runPath) return json(200, checkedRun());
      if (req.method === "POST" && req.path === sendPath) {
        return apiErr(409, "LEASH_ASK_UNRESOLVED", "1 line(s) still need your decision before the team can be paid.", {
          open_asks: [{ line_id: "prl_0002", flags: ["PR-04"] }],
          open_run_flags: ["PR-06"],
        });
      }
      return null;
    });
    const toasts: unknown[] = [];
    const off = on("api-error", (e) => toasts.push(e));
    const user = userEvent.setup();
    renderRun();
    await waitFor(() => expect(payTeam()).toBeEnabled());
    const getsBefore = runGets(calls).length;

    await user.click(payTeam());
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Send payment" }));

    const alert = await screen.findByRole("alert", { name: "Still needs your decision" });
    expect(within(alert).getByText(/Sara/)).toBeInTheDocument();
    expect(within(alert).getByText(/Over 80% of cash received/)).toBeInTheDocument();
    expect(within(screen.getByRole("row", { name: /Sara/ })).getByText("Needs your decision")).toBeInTheDocument();
    await waitFor(() => expect(runGets(calls).length).toBeGreaterThan(getsBefore)); // re-fetched
    expect(sends(calls)).toHaveLength(1);
    expect(toasts).toHaveLength(0);
    off();
  });

  it("ticked run flags feed approve_run_flags", async () => {
    const run = awaitingRun();
    run.lines[0].owner_action = "APPROVED";
    const calls = spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, structuredClone(run)) : null));
    const user = userEvent.setup();
    renderRun();
    const box = await screen.findByRole("checkbox", { name: "Approve: Over 80% of cash received" });
    expect(payTeam()).toBeDisabled();
    expect(screen.getByText(/Approve the 80% check above/)).toBeInTheDocument();
    await user.click(box);
    expect(payTeam()).toBeEnabled();
    await user.click(payTeam());
    expect(within(await screen.findByRole("dialog")).getByText(/Send \$720\.00 to 3 people/)).toBeInTheDocument();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Send payment" }));
    await waitFor(() => expect(sends(calls)).toHaveLength(1));
    expect((sends(calls)[0].body as PayrollSendRequest).approve_run_flags).toEqual(["PR-06"]);
  });

  it("LINE_NOT_CHECKED shows on the row, quietly", async () => {
    spyTransport((req) =>
      req.method === "PATCH" ? apiErr(409, "LINE_NOT_CHECKED", "This line could not be checked, so it cannot be approved. Refresh it.") : null,
    );
    const toasts: unknown[] = [];
    const off = on("api-error", (e) => toasts.push(e));
    const user = userEvent.setup();
    renderRun();
    await user.click(await screen.findByRole("button", { name: "Approve Omar" }));
    const row = screen.getByRole("row", { name: /Omar/ });
    expect(await within(row).findByText(/could not be checked/)).toBeInTheDocument();
    expect(toasts).toHaveLength(0);
    off();
  });

  it("PAYPAL_UNAVAILABLE says so and keeps checking", async () => {
    let sent = false;
    const calls = spyTransport((req) => {
      if (req.method === "GET" && req.path === runPath) return json(200, sent ? sentRun() : checkedRun());
      if (req.method === "POST" && req.path === sendPath) {
        sent = true; // the server marks the run SENT (batch UNKNOWN) before answering 503
        return apiErr(503, "PAYPAL_UNAVAILABLE", "PayPal did not confirm the payout. Reconcile the run.");
      }
      return null;
    });
    const toasts: unknown[] = [];
    const off = on("api-error", (e) => toasts.push(e));
    const user = userEvent.setup();
    renderRun();
    await waitFor(() => expect(payTeam()).toBeEnabled());
    await user.click(payTeam());
    vi.useFakeTimers();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Send payment" }));
    await tick(0);
    expect(
      screen.getByText("PayPal didn't confirm. The batch may still go through; this page will keep checking."),
    ).toBeInTheDocument();
    const n = runGets(calls).length;
    await tick(3000);
    await tick(3000);
    expect(runGets(calls).length).toBe(n + 2);
    expect(screen.queryByRole("button", { name: "Pay team" })).not.toBeInTheDocument();
    expect(toasts).toHaveLength(0);
    off();
  });

  it("no edits once the run is sent", async () => {
    spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, sentRun()) : null));
    renderRun();
    expect(await screen.findByText(/Sent to PayPal/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve|Exclude|Undo/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pay team" })).not.toBeInTheDocument();
  });

  it("run helpers", () => {
    const run = awaitingRun();
    expect(openAsks(run).lines.map((l) => l.line_id)).toEqual(["prl_0001"]);
    expect(openAsks(run).runFlags).toEqual(["PR-06"]);
    expect(openAsks(run, ["PR-06"]).runFlags).toEqual([]);
    expect(payBlockers(run, [])).toEqual(["1 line still needs your decision", "Approve the 80% check above"]);
    expect(payBlockers(checkedRun(), [])).toEqual([]);
    const denied = checkedRun();
    denied.run_decision = "DENY";
    expect(payBlockers(denied, [])).toEqual(["Leash denied this run"]);
    expect(parseOpenAsks({ open_asks: ["prl_1", { line_id: "prl_2", flags: [] }], open_run_flags: ["PR-06"] })).toEqual({
      lineIds: ["prl_1", "prl_2"],
      runFlags: ["PR-06"],
    });
    expect(parseOpenAsks(null)).toEqual({ lineIds: [], runFlags: [] });
    const k = sendKey("prn_01JABCDEFGHJKMNPQRSTVWXYZ0");
    expect(k).toBe(sendKey("prn_01JABCDEFGHJKMNPQRSTVWXYZ0"));
    expect(k).not.toBe(sendKey("prn_other"));
    expect(k.length).toBeLessThanOrEqual(64);
    expect(k.startsWith("send-prn_01JABCDEFGHJKMNPQRSTVWXYZ0-")).toBe(true);
  });
});

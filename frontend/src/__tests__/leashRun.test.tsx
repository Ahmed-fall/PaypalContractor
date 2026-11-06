import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { on } from "../lib/events";
import { api, setTransport, type Transport, type TransportRequest, type TransportResponse } from "../api/client";
import { setDemoKey } from "../api/demoKey";
import { mockTransport, resetMockState, setMockLatency } from "../api/mocks";
import PayrollRunPage from "../components/Leash/PayrollRunPage";
import {
  canApprove,
  isFinished,
  needsRecheck,
  openAsks,
  parseOpenAsks,
  payBlockers,
  runFlagLabel,
  runTotalsCents,
  sendKey,
} from "../components/Leash/run";
import type { PayrollLineOut, PayrollRunOut, PayrollSendRequest } from "../types/contracts";
import awaiting from "../../../shared/fixtures/leash/payroll_run_awaiting_approval.json";
import afterExclude from "../../../shared/fixtures/leash/payroll_run_after_exclude.json";

const router = { v7_startTransition: true, v7_relativeSplatPath: true } as const;
const RUN = "prn_0001";
const runPath = `/leash/payroll/runs/${RUN}`;
const sendPath = `${runPath}/send`;


const LINE_IDS: Record<string, string> = { Omar: "prl_0001", Sara: "prl_0002", Youssef: "prl_0003" };

/** A line's cells: AG Grid splits a row with pinned columns into left / middle / right row elements. */
function lineRow(name: keyof typeof LINE_IDS) {
  const parts = () => Array.from(document.querySelectorAll<HTMLElement>(`[row-id="${LINE_IDS[name]}"]`));
  const first = <T,>(find: (el: HTMLElement) => T | null): T | null => {
    for (const el of parts()) {
      const hit = find(el);
      if (hit) return hit;
    }
    return null;
  };
  return {
    queryByText: (t: string | RegExp) => first((el) => within(el).queryByText(t)),
    getByText: (t: string | RegExp) => {
      const hit = first((el) => within(el).queryByText(t));
      if (!hit) throw new Error(`No "${String(t)}" in ${name}'s row`);
      return hit;
    },
    findByText: (t: string | RegExp) => waitFor(() => lineRow(name).getByText(t)),
    getByRole: (role: string, opts?: { name: string | RegExp }) => {
      const hit = first((el) => within(el).queryByRole(role, opts));
      if (!hit) throw new Error(`No ${role} in ${name}'s row`);
      return hit;
    },
  };
}

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

  it("mock: another run id is a 404 for get, patch and send", async () => {
    await expect(api.payrollRun("prn_9999")).rejects.toMatchObject({ status: 404 });
    await expect(api.updatePayrollLine("prn_9999", "prl_0001", { owner_action: "EXCLUDED" })).rejects.toMatchObject({ status: 404 });
    await expect(api.sendPayroll("prn_9999", { idempotency_key: "k", confirm: true, approve_run_flags: [] })).rejects.toMatchObject({ status: 404 });
    expect((await api.payrollRun(RUN)).run_id).toBe(RUN);
  });

  it("mock: an unticked run flag blocks the send even when the run decision is not ASK", async () => {
    const run = await api.payrollRun(RUN);
    run.run_decision = "ALLOW";
    run.lines.forEach((l) => (l.owner_action = "APPROVED"));
    await expect(api.sendPayroll(RUN, { idempotency_key: "k", confirm: true, approve_run_flags: [] })).rejects.toMatchObject({
      status: 409,
      details: { open_run_flags: ["PR-06"] },
    });
  });

  it("mock: an owner action lands in the decision log", async () => {
    await api.updatePayrollLine(RUN, "prl_0001", { owner_action: "EXCLUDED" });
    const log = await api.decisions({});
    expect(log.items[0]).toMatchObject({ actor: "owner", rule_ids: ["OWNER"], subject_id: "prl_0001" });
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
    // Omar's PR-01 risk is never approved by an "Undo": he comes back only through Approve.
    expect(screen.queryByRole("button", { name: "Undo exclude Omar" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve Omar" })).toBeInTheDocument();

    await user.click(payTeam());
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("Send $420.00 to 2 people through PayPal Payouts? This can't be undone."),
    ).toBeInTheDocument();

    vi.useFakeTimers();
    fireEvent.click(within(dialog).getByRole("button", { name: "Send payment" }));
    await tick(0);
    expect(sends(calls)).toHaveLength(1);
    expect(sends(calls)[0].query).toEqual({ expected_total: "420.00" });
    expect(screen.getByText("Sent to PayPal. Batch 5UXD2E8A7EBQJ")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Exclude/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pay team" })).not.toBeInTheDocument();

    await tick(3000);
    await tick(3000);
    expect(screen.getByText("Paid: 2 people, $420.00")).toBeInTheDocument();
    const sara = lineRow("Sara");
    const youssef = lineRow("Youssef");
    expect(sara.getByText("Paid")).toBeInTheDocument();
    expect(youssef.getByText("Paid")).toBeInTheDocument();

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
    // Both clicks land before React commits the disabled state: only the ref can stop the second.
    act(() => {
      confirm.click();
      confirm.click();
    });
    await waitFor(() => expect(sends(calls)).toHaveLength(1));
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
    expect(await screen.findByText(/PayPal didn't get it. You can try again./)).toBeInTheDocument();

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
    expect(lineRow("Omar").getByText("Leash couldn't check this line yet")).toBeInTheDocument();
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
    expect(lineRow("Sara").getByText("Needs your decision")).toBeInTheDocument();
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
    const row = lineRow("Omar");
    expect(await row.findByText(/could not be checked/)).toBeInTheDocument();
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

  it("test_excluded_ask_line_has_no_undo (C1)", async () => {
    const run = checkedRun(); // Omar (ASK, PR-01) excluded
    run.lines[2].owner_action = "EXCLUDED"; // Youssef (ALLOW) excluded too
    run.lines.push({
      ...run.lines[1],
      line_id: "prl_0009",
      member_name: "Dina",
      decision: "DENY",
      flags: ["PR-07"],
      owner_action: "EXCLUDED",
    });
    const calls = spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, structuredClone(run)) : null));
    const user = userEvent.setup();
    renderRun();
    expect(await screen.findByRole("button", { name: "Approve Omar" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undo exclude Omar" })).not.toBeInTheDocument();
    expect(lineRow("Omar").getByText("Payout email changed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undo exclude Youssef" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Dina/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Undo exclude Youssef" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.path).toBe(`${runPath}/lines/prl_0003`);
    expect(patch.body).toEqual({ owner_action: "APPROVED" });
  });

  it("test_modal_refuses_when_server_total_differs (C2)", async () => {
    const run = checkedRun();
    run.total = "500.00"; // the server total no longer matches the lines on screen
    const calls = spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, structuredClone(run)) : null));
    const user = userEvent.setup();
    renderRun();
    await waitFor(() => expect(payTeam()).toBeEnabled());
    await user.click(payTeam());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText(/Refresh the run/)).toBeInTheDocument();
    expect(sends(calls)).toHaveLength(0);
  });

  it("test_changed_total_409_refetches_and_closes (C2)", async () => {
    let changed = false;
    const calls = spyTransport((req) => {
      if (req.method === "GET" && req.path === runPath) {
        const r = checkedRun();
        if (changed) {
          r.total = "500.00";
          r.lines[1].gross = "300.00";
        }
        return json(200, r);
      }
      if (req.method === "POST" && req.path === sendPath) {
        changed = true; // Sara's rate was edited after the page loaded
        return apiErr(409, "RUN_NOT_SENDABLE", "The payroll run changed. Refresh it and try again.");
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
    expect(await screen.findByText("The payroll run changed. Refresh it and try again.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText("$500.00")).toBeInTheDocument();
    expect(runGets(calls).length).toBeGreaterThan(getsBefore);
    expect(sends(calls)[0].query).toEqual({ expected_total: "420.00" });
    expect(toasts).toHaveLength(0);
    off();
  });

  it("test_rows_locked_while_confirming (I1)", async () => {
    spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, checkedRun()) : null));
    const user = userEvent.setup();
    renderRun();
    await waitFor(() => expect(payTeam()).toBeEnabled());
    expect(screen.getByRole("button", { name: "Exclude Sara" })).toBeEnabled();
    await user.click(payTeam());
    await screen.findByRole("dialog");
    await waitFor(() => expect(screen.getByRole("button", { name: "Exclude Sara" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Approve Omar" })).toBeDisabled();
  });

  it("test_ticked_flags_reset_after_edits (I2)", async () => {
    const user = userEvent.setup();
    renderRun();
    await user.click(await screen.findByRole("button", { name: "Approve Omar" }));
    const box = await screen.findByRole("checkbox", { name: "Approve: Over 80% of cash received" });
    await user.click(box);
    await waitFor(() => expect(payTeam()).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Exclude Omar" }));
    expect(await screen.findByText("$420.00")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve Omar" }));
    expect(await screen.findByText("$720.00")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Approve: Over 80% of cash received" })).not.toBeChecked();
    expect(payTeam()).toBeDisabled();
  });

  it("test_run_id_change_resets_state (I3)", async () => {
    function Go() {
      const navigate = useNavigate();
      return (
        <button type="button" onClick={() => navigate("/leash/payroll/prn_0002")}>
          Go to run 2
        </button>
      );
    }
    spyTransport((req) => {
      const m = /^\/leash\/payroll\/runs\/([^/]+)$/.exec(req.path);
      if (req.method === "GET" && m) {
        const r = awaitingRun();
        r.run_id = m[1];
        r.lines[0].owner_action = "APPROVED";
        return json(200, r);
      }
      return null;
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter future={router} initialEntries={[`/leash/payroll/${RUN}`]}>
        <Routes>
          <Route
            path="/leash/payroll/:runId"
            element={
              <>
                <Go />
                <PayrollRunPage />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("checkbox", { name: "Approve: Over 80% of cash received" }));
    expect(payTeam()).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Go to run 2" }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Approve: Over 80% of cash received" })).not.toBeChecked());
    expect(payTeam()).toBeDisabled();
  });

  it("test_sent_state_survives_failed_refetch (I4)", async () => {
    let sentNow = false;
    const calls = spyTransport((req) => {
      if (req.method === "GET" && req.path === runPath) return sentNow ? apiErr(500, "INTERNAL_ERROR", "boom") : json(200, checkedRun());
      if (req.method === "POST" && req.path === sendPath) sentNow = true; // the mock answers 202
      return null;
    });
    await mockTransport({ method: "PATCH", path: `${runPath}/lines/prl_0001`, query: {}, body: { owner_action: "EXCLUDED" }, form: null, headers: { "X-Demo-Key": "demo" } });
    const user = userEvent.setup();
    renderRun();
    await waitFor(() => expect(payTeam()).toBeEnabled());
    await user.click(payTeam());
    vi.useFakeTimers();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Send payment" }));
    await tick(0);
    expect(screen.getByText("Sent to PayPal. Batch 5UXD2E8A7EBQJ")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pay team" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Exclude/ })).not.toBeInTheDocument();
    const n = runGets(calls).length;
    await tick(3000);
    expect(runGets(calls).length).toBe(n + 1);
  });

  it("test_only_paypal_unavailable_keeps_checking (I5)", async () => {
    let code = "SERVICE_UNAVAILABLE";
    spyTransport((req) => {
      if (req.method === "GET" && req.path === runPath) return json(200, checkedRun());
      if (req.method === "POST" && req.path === sendPath) return apiErr(503, code, "The service is down.");
      return null;
    });
    const user = userEvent.setup();
    renderRun();
    await waitFor(() => expect(payTeam()).toBeEnabled());
    await user.click(payTeam());
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Send payment" }));
    expect(await screen.findByText(/The service is down./)).toBeInTheDocument();
    expect(screen.queryByText(/this page will keep checking/)).not.toBeInTheDocument();

    code = "PAYPAL_UNAVAILABLE"; // but the re-read run is not SENT: PayPal never got it
    await user.click(payTeam());
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Send payment" }));
    expect(await screen.findByText("PayPal didn't get it. You can try again.")).toBeInTheDocument();
    expect(screen.queryByText(/this page will keep checking/)).not.toBeInTheDocument();
  });

  it("test_mock_send_matches_server (I7)", async () => {
    const send = (key: string, query: Record<string, string> = {}) =>
      mockTransport({
        method: "POST",
        path: sendPath,
        query,
        body: { confirm: true, approve_run_flags: [], idempotency_key: key },
        form: null,
        headers: { "X-Demo-Key": "demo" },
      });
    const open = await send("k1");
    expect(open.status).toBe(409);
    expect((open.body as { error: { details: unknown } }).error.details).toEqual({
      open_asks: [{ line_id: "prl_0001", flags: ["PR-01"] }],
      open_run_flags: ["PR-06"],
    });
    await mockTransport({ method: "PATCH", path: `${runPath}/lines/prl_0001`, query: {}, body: { owner_action: "EXCLUDED" }, form: null, headers: { "X-Demo-Key": "demo" } });
    const stale = await send("k1", { expected_total: "720.00" });
    expect(stale.status).toBe(409);
    expect((stale.body as { error: { code: string } }).error.code).toBe("RUN_NOT_SENDABLE");
    expect((await send("k1", { expected_total: "420.00" })).status).toBe(202);
    expect((await send("k1")).status).toBe(202); // same key: the stored batch
    const other = await send("k2");
    expect(other.status).toBe(409);
    expect((other.body as { error: { code: string } }).error.code).toBe("RUN_NOT_SENDABLE");
  });

  it("test_unchecked_run_blocks_or_rechecks (M1, M2)", async () => {
    const prepared = checkedRun();
    prepared.status = "PREPARED";
    expect(payBlockers(prepared, [])).toEqual(["Leash hasn't checked this run yet"]);
    const askNoFlags = checkedRun();
    askNoFlags.run_decision = "ASK";
    askNoFlags.run_flags = [];
    expect(payBlockers(askNoFlags, [])).toEqual(["Leash hasn't checked this run yet"]);

    // DEFAULT as the only blocker: the click goes through and the server re-checks on send.
    const run = checkedRun();
    run.run_decision = "ASK";
    run.run_flags = ["DEFAULT"];
    expect(payBlockers(run, [])).toEqual([]);
    expect(needsRecheck(run)).toBe(true);
    const calls = spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, structuredClone(run)) : null));
    const user = userEvent.setup();
    renderRun();
    expect(await screen.findByText(/Pay team asks Leash to check it again/)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByText(/Approve: Not checked yet/)).not.toBeInTheDocument();
    expect(payTeam()).toBeEnabled();
    await user.click(payTeam());
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Send payment" }));
    await waitFor(() => expect(sends(calls)).toHaveLength(1));
    expect((sends(calls)[0].body as PayrollSendRequest).approve_run_flags).toEqual([]);
  });

  it("test_unclaimed_lines_are_named (M5)", async () => {
    const run = sentRun();
    run.status = "PARTIAL";
    run.lines[1].payout_status = "SUCCESS";
    run.lines[2].payout_status = "UNCLAIMED";
    spyTransport((req) => (req.method === "GET" && req.path === runPath ? json(200, structuredClone(run)) : null));
    renderRun();
    expect(
      await screen.findByText("Partly paid: 1 of 2 people paid, $220.00. Youssef hasn't claimed it yet. Check the lines below."),
    ).toBeInTheDocument();
    expect(lineRow("Youssef").getByText("Youssef hasn't claimed it yet")).toBeInTheDocument();
  });

  it("grid root carries the label, PR-06 uses the policy limit (M7, M8)", async () => {
    expect(runFlagLabel("PR-06", "75")).toBe("Over 75% of cash received");
    expect(runFlagLabel("PR-01", "75")).toBe("Payout email changed");
    expect(runFlagLabel("DEFAULT", "80")).toBe("Leash couldn't check this run yet");
    renderRun();
    await screen.findByRole("button", { name: "Exclude Omar" });
    await waitFor(() => expect(screen.getAllByLabelText("Payroll lines").length).toBeGreaterThan(1));
  });

  it("sendKey refuses a key over 64 chars (M9)", () => {
    expect(() => sendKey("prn_" + "x".repeat(60))).toThrow();
  });
});

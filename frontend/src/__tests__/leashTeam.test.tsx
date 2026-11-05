import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api, setTransport, type Transport, type TransportRequest } from "../api/client";
import { setDemoKey } from "../api/demoKey";
import { mockTransport, resetMockState, setMockLatency } from "../api/mocks";
import { flagLabel, maskEmail, ownerActionLabel, payoutLabel } from "../components/Leash/format";
import { discardTimesheet, listPayrollRuns, savePolicyDraft } from "../components/Leash/api";
import TeamPage from "../components/Leash/TeamPage";

const router = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

function renderTeam() {
  return render(
    <MemoryRouter future={router}>
      <TeamPage />
    </MemoryRouter>,
  );
}

describe("Leash Team screen (WP-5.5)", () => {
  beforeEach(() => {
    setMockLatency(0);
    resetMockState();
    setTransport(mockTransport);
    setDemoKey("demo");
  });

  it("renders roster from fixtures", async () => {
    renderTeam();
    const grid = await screen.findByRole("region", { name: "Team members" });
    expect(await within(grid).findByText("Omar")).toBeInTheDocument();
    expect(within(grid).getByText("sb***@personal.example.com")).toBeInTheDocument();
    expect(within(grid).queryByText("sb-omar.new@personal.example.com")).not.toBeInTheDocument();
    expect(within(grid).getByText("$25.00")).toBeInTheDocument();
  });

  it("email change shows the PR-01 warning before saving", async () => {
    const calls: TransportRequest[] = [];
    const spy: Transport = async (req) => {
      calls.push(req);
      return mockTransport(req);
    };
    setTransport(spy);
    const user = userEvent.setup();
    renderTeam();
    await user.click(await screen.findByRole("button", { name: "Edit Omar" }));
    const email = screen.getByLabelText("Payout email", { selector: "#edit-email" });
    await user.clear(email);
    await user.type(email, "omar.third@personal.example.com");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText("Changing Omar's payout email puts their next payment on hold until you confirm it (Leash check PR-01)."),
    ).toBeInTheDocument();
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);

    await user.click(screen.getByRole("button", { name: "Change email" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.body).toMatchObject({ payout_email: "omar.third@personal.example.com" });
  });

  it("a rate-only edit saves without the email warning", async () => {
    const user = userEvent.setup();
    renderTeam();
    await user.click(await screen.findByRole("button", { name: "Edit Omar" }));
    const rate = screen.getByLabelText("Hourly rate", { selector: "#edit-rate" });
    await user.clear(rate);
    await user.type(rate, "30.00");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("$30.00")).toBeInTheDocument();
    expect(screen.queryByText(/Leash check PR-01/)).not.toBeInTheDocument();
  });

  it("add member validates money", async () => {
    const calls: TransportRequest[] = [];
    setTransport(async (req) => {
      calls.push(req);
      return mockTransport(req);
    });
    const user = userEvent.setup();
    renderTeam();
    await screen.findByText("Omar");
    await user.type(screen.getByLabelText("Name"), "Nadia");
    await user.type(screen.getByLabelText("Payout email"), "nadia@personal.example.com");
    await user.type(screen.getByLabelText("Hourly rate"), "12.345");
    await user.click(screen.getByRole("button", { name: "Add member" }));
    expect(await screen.findByText("Enter the rate in dollars and cents, like 25.00.")).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);

    const rate = screen.getByLabelText("Hourly rate");
    await user.clear(rate);
    await user.type(rate, "12.50");
    await user.click(screen.getByRole("button", { name: "Add member" }));
    expect(await screen.findByText("Nadia")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({
      name: "Nadia",
      payout_email: "nadia@personal.example.com",
      hourly_rate: "12.50",
    });
  });

  it("shows the server's duplicate-email error inline", async () => {
    setTransport(async (req) =>
      req.method === "POST"
        ? {
            status: 409,
            body: { error: { code: "PAYOUT_EMAIL_IN_USE", message: "That payout email already belongs to Sara.", details: null, request_id: "r" } },
          }
        : mockTransport(req),
    );
    const user = userEvent.setup();
    renderTeam();
    await screen.findByText("Omar");
    await user.type(screen.getByLabelText("Name"), "Nadia");
    await user.type(screen.getByLabelText("Payout email"), "sara@personal.example.com");
    await user.type(screen.getByLabelText("Hourly rate"), "12.50");
    await user.click(screen.getByRole("button", { name: "Add member" }));
    expect(await screen.findByText("That payout email already belongs to Sara.")).toBeInTheDocument();
  });
});

describe("Leash format helpers", () => {
  it("maskEmail keeps two characters and the domain", () => {
    expect(maskEmail("sb-sara@personal.example.com")).toBe("sb***@personal.example.com");
    expect(maskEmail("a@x.io")).toBe("a***@x.io");
    expect(maskEmail("not-an-email")).toBe("***");
  });

  it("flagLabel maps ids to plain labels", () => {
    const table: Array<[string, string]> = [
      ["PR-01", "Payout email changed"],
      ["PR-02", "Unusual hours"],
      ["PR-03", "First payment"],
      ["PR-04", "Rate differs from roster"],
      ["PR-05", "Unknown or duplicate"],
      ["PR-06", "Over 80% of cash received"],
      ["PR-07", "Already paid"],
      ["DEFAULT", "Not checked yet"],
      ["XYZ-9", "XYZ-9"],
    ];
    for (const [id, label] of table) expect(flagLabel(id)).toBe(label);
    for (const id of ["G-01", "G-02", "G-03", "G-04", "G-05"]) expect(flagLabel(id)).not.toBe(id);
  });

  it("owner action and payout labels are plain words", () => {
    expect(ownerActionLabel("APPROVED")).toBe("Approved");
    expect(ownerActionLabel("EXCLUDED")).toBe("Excluded");
    expect(ownerActionLabel(null)).toBe("");
    expect(payoutLabel("SUCCESS")).toBe("Paid");
    expect(payoutLabel("PENDING")).toBe("Pending");
    expect(payoutLabel(null)).toBe("Not sent");
  });
});

describe("Leash API helpers on the mock", () => {
  beforeEach(() => {
    setMockLatency(0);
    resetMockState();
    setTransport(mockTransport);
    setDemoKey("demo");
  });

  it("lists runs, saves a draft and discards an upload", async () => {
    const runs = await listPayrollRuns();
    expect(runs).toHaveLength(1);
    expect(runs[0].run_id).toBe("prn_0001");

    const policy = { version: 3, rules: [], payroll_margin_limit_pct: "80", email_change_window_days: 7 };
    const draft = await savePolicyDraft(policy);
    expect(draft.status).toBe("DRAFT");
    expect(draft.policy).toEqual(policy);

    expect(await discardTimesheet("tsu_1")).toEqual({ upload_id: "tsu_1", status: "REJECTED" });
  });

  it("the payroll run mock is stateful: exclude, send, poll, paid, and reset", async () => {
    const first = await api.payrollRun("prn_0001");
    expect(first.status).toBe("AWAITING_APPROVAL");
    await expect(api.sendPayroll("prn_0001", { confirm: true, idempotency_key: "k1" })).rejects.toMatchObject({
      status: 409,
      code: "LEASH_ASK_UNRESOLVED",
    });

    const excluded = await api.updatePayrollLine("prn_0001", "prl_0001", { owner_action: "EXCLUDED" });
    expect(excluded.total).toBe("420.00");
    expect((await api.payrollRun("prn_0001")).total).toBe("420.00");

    const sent = await api.sendPayroll("prn_0001", { confirm: true, idempotency_key: "k1" });
    expect(sent.paypal_batch_id).toBe("5UXD2E8A7EBQJ");
    const p1 = await api.payrollRun("prn_0001");
    expect(p1.status).toBe("SENT");
    expect(p1.lines.filter((l) => l.payout_status === "PENDING")).toHaveLength(2);
    expect((await api.payrollRun("prn_0001")).status).toBe("SENT");
    const done = await api.payrollRun("prn_0001");
    expect(done.status).toBe("PAID");
    expect(done.lines.filter((l) => l.payout_status === "SUCCESS").map((l) => l.member_name)).toEqual(["Sara", "Youssef"]);

    resetMockState();
    expect((await api.payrollRun("prn_0001")).status).toBe("AWAITING_APPROVAL");
  });
});

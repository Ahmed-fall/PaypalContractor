import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { on } from "../lib/events";
import { setTransport, type Transport, type TransportRequest } from "../api/client";
import { setDemoKey } from "../api/demoKey";
import { mockTransport, resetMockState, setMockLatency } from "../api/mocks";
import RulesPage from "../components/Leash/RulesPage";
import { describeRule, formFromPolicy, policyFromForm, validateRulesForm } from "../components/Leash/rules";
import activePolicy from "../../../shared/fixtures/leash/active_policy.json";
import type { LeashPolicy, LeashRule } from "../types/contracts";

const router = { v7_startTransition: true, v7_relativeSplatPath: true } as const;
const fixture = activePolicy as unknown as LeashPolicy;
const active: LeashPolicy = {
  version: fixture.version,
  rules: fixture.rules,
  payroll_margin_limit_pct: fixture.payroll_margin_limit_pct,
  email_change_window_days: fixture.email_change_window_days,
};
const rule = (id: string) => active.rules.find((r) => r.id === id) as LeashRule;

function renderRules() {
  return render(
    <MemoryRouter future={router}>
      <RulesPage />
    </MemoryRouter>,
  );
}

describe("rules helpers", () => {
  it("describeRule covers G-01..G-05 and unknown keys", () => {
    expect(describeRule(rule("G-01"))).toBe(
      "Correction invoices of $250.00 or less go out on their own when the clause is quoted and the contract is clear (confidence ≥ 0.90).",
    );
    expect(describeRule(rule("G-02"))).toBe("Correction invoices above $250.00 wait for you.");
    expect(describeRule(rule("G-03"))).toBe("Clients tagged never_auto_contact always wait for you.");
    expect(describeRule(rule("G-04"))).toBe("A second invoice to the same client within 7 days waits for you.");
    expect(describeRule(rule("G-05"))).toBe("Invoices whose line items don't add up are blocked (built in, can't be removed).");
    const unknown: LeashRule = { ...rule("G-02"), id: "X-1", when: { vip_client: true }, outcome: "DENY" };
    expect(describeRule(unknown)).toBe("When vip client is true, Leash blocks it (X-1).");
  });

  it("describeRule handles an unquoted clause, payroll actions and a bad confidence", () => {
    const g1 = rule("G-01");
    expect(describeRule({ ...g1, when: { amount_lte: "100.00", clause_quote_present: false } })).toBe(
      "Correction invoices of $100.00 or less go out on their own when the clause is not quoted.",
    );
    const pay = describeRule({ ...g1, applies_to: "SEND_PAYROLL_BATCH", outcome: "ASK", when: { amount_gt: "5000.00" } });
    expect(pay).toBe("Payroll runs above $5,000.00 wait for you.");
    expect(pay.toLowerCase()).not.toContain("invoice");
    expect(describeRule({ ...g1, when: { amount_lte: "10.00", confidence_gte: "high" } })).toContain("confidence ≥ high");
  });

  it("policyFromForm never includes reserved ids and maps the limit to G-01/G-02 as 2-dp strings", () => {
    const form = { ...formFromPolicy(active), autoSendLimit: "300.5" };
    const policy = policyFromForm(form, active);
    expect(policy.rules.map((r) => r.id)).toEqual(["G-01", "G-02", "G-03", "G-04"]);
    expect(policy.rules.find((r) => r.id === "G-01")?.when.amount_lte).toBe("300.50");
    expect(policy.rules.find((r) => r.id === "G-02")?.when.amount_gt).toBe("300.50");
    // Reserved ids smuggled in through the base policy are dropped too.
    const dirty: LeashPolicy = {
      ...active,
      rules: [...active.rules, { ...rule("G-05"), id: "DEFAULT" }, { ...rule("G-05"), id: "PAYROLL-CHECKS" }],
    };
    expect(policyFromForm(formFromPolicy(dirty), dirty).rules.map((r) => r.id)).toEqual(["G-01", "G-02", "G-03", "G-04"]);
  });

  it("round-trips the active policy apart from the built-in G-05", () => {
    const rebuilt = policyFromForm(formFromPolicy(active), active);
    expect(rebuilt).toEqual({ ...active, rules: active.rules.filter((r) => r.id !== "G-05") });
  });

  it("turns G-03 and G-04 off and keeps other rules", () => {
    const custom: LeashRule = { ...rule("G-02"), id: "R-9", when: { amount_gt: "900.00" } };
    const base: LeashPolicy = { ...active, rules: [...active.rules, custom] };
    const form = { ...formFromPolicy(base), neverAutoContact: false, repeatWindowDays: "0" };
    expect(policyFromForm(form, base).rules.map((r) => r.id)).toEqual(["G-01", "G-02", "R-9"]);
  });

  it("validates what the server would refuse", () => {
    const ok = formFromPolicy(active);
    expect(validateRulesForm(ok)).toEqual({});
    const bad = validateRulesForm({
      ...ok,
      autoSendLimit: "12.345",
      minConfidence: "1.5",
      repeatWindowDays: "-1",
      emailWindowDays: "366",
      marginLimitPct: "0",
    });
    expect(Object.keys(bad).sort()).toEqual(["autoSendLimit", "emailWindowDays", "marginLimitPct", "minConfidence", "repeatWindowDays"]);
    expect(validateRulesForm({ ...ok, emailWindowDays: "365", marginLimitPct: "100", repeatWindowDays: "0" })).toEqual({});
  });
});

describe("Leash Rules screen (WP-5.5)", () => {
  beforeEach(() => {
    setMockLatency(0);
    resetMockState();
    setTransport(mockTransport);
    setDemoKey("demo");
  });

  it("page shows active cards", async () => {
    renderRules();
    const list = await screen.findByRole("list", { name: "Rule cards" });
    expect(within(list).getByText("Correction invoices above $250.00 wait for you.")).toBeInTheDocument();
    expect(within(list).getByText(/built in, can't be removed/)).toBeInTheDocument();
    expect(screen.getByText("Version 2")).toBeInTheDocument();
  });

  it("preview then activate flow", async () => {
    const calls: TransportRequest[] = [];
    const spy: Transport = async (req) => {
      calls.push(req);
      return mockTransport(req);
    };
    setTransport(spy);
    const user = userEvent.setup();
    renderRules();
    const limit = await screen.findByLabelText("Auto-send limit (USD)");
    await user.clear(limit);
    await user.type(limit, "400");
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Preview" }));

    const activate = await screen.findByRole("button", { name: "Activate version 3" });
    const post = calls.find((c) => c.method === "POST" && c.path === "/leash/policy");
    const sent = post?.body as LeashPolicy;
    expect(sent.rules.map((r) => r.id)).not.toContain("G-05");
    expect(sent.rules[0].when.amount_lte).toBe("400.00");
    expect(screen.getByText("DRAFT")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/leash/policy/activate")).toBe(false);

    await user.click(activate);
    await waitFor(() => expect(calls.some((c) => c.path === "/leash/policy/activate")).toBe(true));
    expect(calls.find((c) => c.path === "/leash/policy/activate")?.body).toEqual({ version: 3 });
    expect(await screen.findByText("Version 3 is now active.")).toBeInTheDocument();
    expect(await screen.findByText("Version 3")).toBeInTheDocument();
    expect(screen.getByText("Correction invoices of $400.00 or less go out on their own when the clause is quoted and the contract is clear (confidence ≥ 0.90).")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Activate version/ })).not.toBeInTheDocument();
  });

  it("blocks a bad form before sending anything", async () => {
    const calls: TransportRequest[] = [];
    setTransport(async (req) => {
      calls.push(req);
      return mockTransport(req);
    });
    const user = userEvent.setup();
    renderRules();
    const days = await screen.findByLabelText("Email-change hold (days)");
    await user.clear(days);
    await user.type(days, "400");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("Enter whole days from 0 to 365.")).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("shows a server 422 beside its field", async () => {
    setTransport(async (req) =>
      req.method === "POST" && req.path === "/leash/policy"
        ? {
            status: 422,
            body: {
              error: {
                code: "VALIDATION_ERROR",
                message: "policy failed validation",
                details: { fields: [{ field: "email_change_window_days", msg: "must be between 0 and 365" }] },
              },
            },
          }
        : mockTransport(req),
    );
    const user = userEvent.setup();
    renderRules();
    await user.click(await screen.findByRole("button", { name: "Preview" }));
    expect(await screen.findByText("must be between 0 and 365")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Activate version/ })).not.toBeInTheDocument();
  });

  it("compiler 501 shows the fallback message", async () => {
    setTransport(async (req) =>
      req.path === "/leash/policy/compile"
        ? { status: 501, body: { error: { code: "COMPILER_UNAVAILABLE", message: "Use the plain policy form.", details: null } } }
        : mockTransport(req),
    );
    const user = userEvent.setup();
    renderRules();
    await user.type(await screen.findByLabelText(/Describe the rules/), "ask me about anything over 500");
    await user.click(screen.getByRole("button", { name: "Compile with AI" }));
    expect(await screen.findByText("The AI rule writer isn't available yet. Use the form below.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview" })).toBeEnabled();
  });

  it("matches a 422 field named with a dotted prefix", async () => {
    setTransport(async (req) =>
      req.method === "POST" && req.path === "/leash/policy"
        ? {
            status: 422,
            body: {
              error: {
                code: "VALIDATION_ERROR",
                message: "bad",
                details: { fields: [{ field: "body.payroll_margin_limit_pct", msg: "must be at most 100" }] },
              },
            },
          }
        : mockTransport(req),
    );
    const user = userEvent.setup();
    renderRules();
    await user.click(await screen.findByRole("button", { name: "Preview" }));
    const msg = await screen.findByText("must be at most 100");
    expect(msg.id).toBe("rf-margin-error");
    const field = screen.getByLabelText("Payroll cash limit (%)");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAttribute("aria-describedby", "rf-margin-error");
  });

  it("a 409 on activate shows inline and emits no api-error", async () => {
    const seen: unknown[] = [];
    const off = on("api-error", (e) => seen.push(e));
    setTransport(async (req) =>
      req.path === "/leash/policy/activate"
        ? { status: 409, body: { error: { code: "POLICY_VERSION_CONFLICT", message: "Another policy was just activated.", details: null } } }
        : mockTransport(req),
    );
    const user = userEvent.setup();
    renderRules();
    await user.click(await screen.findByRole("button", { name: "Preview" }));
    await user.click(await screen.findByRole("button", { name: "Activate version 3" }));
    expect(await screen.findByText("Another version was just activated. Reload to see it.")).toBeInTheDocument();
    off();
    expect(seen).toEqual([]);
  });

  it("keeps the success message when the reload after activation fails", async () => {
    let activated = false;
    setTransport(async (req) => {
      if (req.path === "/leash/policy/activate") activated = true;
      if (activated && req.method === "GET" && req.path === "/leash/policy") {
        return { status: 500, body: { error: { code: "INTERNAL", message: "boom", details: null } } };
      }
      return mockTransport(req);
    });
    const user = userEvent.setup();
    renderRules();
    await user.click(await screen.findByRole("button", { name: "Preview" }));
    await user.click(await screen.findByRole("button", { name: "Activate version 3" }));
    expect(await screen.findByText(/Version 3 is now active\./)).toBeInTheDocument();
    expect(screen.queryByText("That draft is gone. Preview again.")).not.toBeInTheDocument();
  });

  it("editing the form clears a stale draft", async () => {
    const user = userEvent.setup();
    renderRules();
    await user.click(await screen.findByRole("button", { name: "Preview" }));
    expect(await screen.findByRole("button", { name: "Activate version 3" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Auto-send limit (USD)"), "1");
    expect(screen.queryByRole("button", { name: /Activate version/ })).not.toBeInTheDocument();
  });

  it("a compiled draft fills the form and renders its rule cards", async () => {
    const user = userEvent.setup();
    renderRules();
    await user.type(await screen.findByLabelText(/Describe the rules/), "small ones go out on their own");
    await user.click(screen.getByRole("button", { name: "Compile with AI" }));
    const cards = await screen.findByRole("list", { name: "Draft cards" });
    expect(within(cards).getByText("Correction invoices above $250.00 wait for you.")).toBeInTheDocument();
    expect(screen.getByLabelText("Auto-send limit (USD)")).toHaveValue("250.00");
    expect(screen.getByRole("button", { name: "Activate version 3" })).toBeInTheDocument();
  });

  it("shows a retry state when the rules cannot be loaded", async () => {
    let fail = true;
    setTransport(async (req) =>
      fail && req.method === "GET" && req.path === "/leash/policy"
        ? { status: 500, body: { error: { code: "INTERNAL", message: "boom", details: null } } }
        : mockTransport(req),
    );
    const seen: unknown[] = [];
    const off = on("api-error", (e) => seen.push(e));
    const user = userEvent.setup();
    renderRules();
    expect(await screen.findByText(/Couldn't load the rules\./)).toBeInTheDocument();
    off();
    expect(seen).toEqual([]);
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("list", { name: "Rule cards" })).toBeInTheDocument();
  });
});

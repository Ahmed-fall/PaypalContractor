/**
 * Mock backend for VITE_USE_MOCKS=true (plan WP-3.1, A.7).
 *
 * Seeds from shared/fixtures (owned by P4/P5) and keeps state in memory so the full
 * demo story works offline: upload Acme -> activate -> simulator $850 five days late
 * -> Leash ALLOW (G-01) auto-sends -> ledger + margin. It mirrors the A.2 wire format
 * and the A.4 error body, enforces X-Demo-Key on writes, and replays idempotency keys.
 * It is a development aid only: the production path never imports this file.
 */
import type {
  ContractActivateRequest,
  ContractActivateResponse,
  ContractExtractResponse,
  DecisionLogEntry,
  DecisionLogResponse,
  DiscrepancyEvaluationResult,
  DiscrepancyType,
  GuardResult,
  InvoiceLineItemSpec,
  LedgerResponse,
  LedgerRow,
  MarginResponse,
  MarginRow,
  PersistedContractExtraction,
  RemediationInvoiceRecord,
  RemediationRequest,
  RemediationResponse,
  SimulatorPaymentRequest,
  SimulatorPaymentResponse,
} from "../types/contracts";
import type { Transport, TransportRequest, TransportResponse } from "./client";
import { daysBetween } from "../lib/dates";
import { centsToString, formatMoney, sumMoney, toCents } from "../lib/money";

// ------------------------------------------------------------------ fixtures

// Globs resolve to {} when shared/ is outside the build context (Docker), so a
// production build never fails here; mock mode is simply unavailable there.
const jsonFixtures = import.meta.glob("../../../shared/fixtures/json/*.json", {
  eager: true,
  import: "default",
}) as Record<string, unknown>;
const leashFixtures = import.meta.glob("../../../shared/fixtures/leash/*.json", {
  eager: true,
  import: "default",
}) as Record<string, unknown>;
const contractTexts = import.meta.glob("../../../shared/fixtures/contracts/*.md", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;

function pick<T>(store: Record<string, unknown>, name: string): T | null {
  const key = Object.keys(store).find((k) => k.endsWith(`/${name}`));
  return key ? (structuredClone(store[key]) as T) : null;
}

/** Raw markdown of a canonical contract, for the workbench text pane in mock mode. */
export function fixtureContractText(stem: string): string | null {
  const key = Object.keys(contractTexts).find((k) => k.endsWith(`/${stem}.md`));
  return key ? contractTexts[key] : null;
}

export const FIXTURE_STEMS = ["Acme_Standard", "Globex_LateFee", "Initech_MultiMilestone"] as const;

const EXTRACT_BY_STEM: Record<string, string> = {
  acme: "acme_extract.json",
  globex: "globex_extract.json",
  initech: "initech_extract.json",
};

// ------------------------------------------------------------------ state

interface ContractRecord {
  contract_id: string;
  status: "DRAFT" | "ACTIVE";
  source_filename: string;
  extraction: PersistedContractExtraction;
  client_tags: string[];
}

interface MockState {
  contracts: Map<string, ContractRecord>;
  rows: LedgerRow[];
  margin: MarginRow[];
  decisions: DecisionLogEntry[];
  idempotency: Map<string, unknown>;
  seq: number;
}

let latencyMs = 400;
const PERSIST_KEY = "fd.mockState.v1";
let state: MockState = restore() ?? seed();

/** Keep the demo story across page reloads (sessionStorage; falls back to memory). */
function persist(): void {
  try {
    sessionStorage.setItem(
      PERSIST_KEY,
      JSON.stringify({ ...state, contracts: [...state.contracts.values()], idempotency: [...state.idempotency.entries()] }),
    );
  } catch {
    /* memory only */
  }
}

function restore(): MockState | null {
  try {
    const raw = sessionStorage.getItem(PERSIST_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Omit<MockState, "contracts" | "idempotency"> & {
      contracts: ContractRecord[];
      idempotency: [string, unknown][];
    };
    return { ...s, contracts: new Map(s.contracts.map((c) => [c.contract_id, c])), idempotency: new Map(s.idempotency) };
  } catch {
    return null;
  }
}

/** Tests set this to 0. */
export function setMockLatency(ms: number): void {
  latencyMs = ms;
}

/** Restore the seeded demo state (also what POST /demo/reset does). */
export function resetMockState(): void {
  state = seed();
  try {
    sessionStorage.removeItem(PERSIST_KEY);
  } catch {
    /* ignore */
  }
}

function seed(): MockState {
  const contracts = new Map<string, ContractRecord>();
  const rows: LedgerRow[] = [];

  for (const stem of ["acme", "globex", "initech"]) {
    const ex = pick<ContractExtractResponse>(jsonFixtures, EXTRACT_BY_STEM[stem]);
    const ledger = pick<LedgerResponse>(jsonFixtures, `${stem}_ledger.json`);
    if (!ex || !ledger) continue;
    contracts.set(ex.contract_id, {
      contract_id: ex.contract_id,
      status: "ACTIVE",
      source_filename: ex.source_filename,
      extraction: ex.extraction,
      // Globex is the G-03 example: always ask before contacting this client.
      client_tags: stem === "globex" ? ["never_auto_contact"] : [],
    });
    for (const r of ledger.rows) {
      rows.push({
        ...r,
        leash_decision: r.leash_decision ?? null,
        remediation_sent_by: r.remediation_sent_by ?? null,
        captured_currency: r.captured_currency ?? null,
      });
    }
  }

  // Acme MS-01 starts unpaid so the simulator can tell the headline story (16.2).
  const acme = rows.find((r) => r.milestone_id === "ms_01");
  if (acme) Object.assign(acme, unpaid());

  const decisions: DecisionLogEntry[] = [];
  let n = 1;
  for (const r of rows) {
    if (r.discrepancy_status !== "PENDING_APPROVAL" || !r.discrepancy_id) continue;
    const tagged = contracts.get(r.contract_id)?.client_tags.includes("never_auto_contact");
    const ruleId = tagged ? "G-03" : "G-02";
    const reason = tagged
      ? `${r.client_name} is tagged never_auto_contact, so Leash asks before sending.`
      : `${formatMoney(r.total_remediation_amount, r.currency)} is over your $250.00 auto-send limit, so Leash is asking you first.`;
    r.leash_decision = "ASK";
    decisions.push({
      decision_id: `lsd_${String(n++).padStart(4, "0")}`,
      created_at: r.captured_at ?? "2026-10-20T00:00:00Z",
      policy_version: 2,
      action_type: "SEND_REMEDIATION_INVOICE",
      subject_type: "discrepancy",
      subject_id: r.discrepancy_id,
      decision: "ASK",
      rule_ids: [ruleId],
      amount: r.total_remediation_amount,
      reason,
      actor: "leash",
    });
  }

  // P5's margin fixture covers MS-01 and MS-03 only; the UI must coalesce the rest.
  const marginFixture = pick<MarginResponse>(leashFixtures, "ledger_margin.json");
  const margin = (marginFixture?.rows ?? []).filter((m) => m.milestone_id !== "ms_01");

  return { contracts, rows, margin, decisions, idempotency: new Map(), seq: 100 };
}

function unpaid(): Partial<LedgerRow> {
  return {
    captured_amount: null,
    captured_currency: null,
    shortfall_amount: "0.00",
    penalty_amount: "0.00",
    total_remediation_amount: "0.00",
    captured_at: null,
    paypal_order_id: null,
    paypal_capture_id: null,
    webhook_verified: false,
    discrepancy_type: null,
    discrepancy_status: "NONE",
    discrepancy_id: null,
    violated_clause_quote: null,
    ai_audit_summary: null,
    remediation_invoice: null,
    leash_decision: null,
    remediation_sent_by: null,
  };
}

// ------------------------------------------------------------------ helpers

function nextId(prefix: string): string {
  state.seq += 1;
  return `${prefix}${state.seq.toString(36).toUpperCase().padStart(6, "0")}`;
}

const ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789";
function paypalId(len = 17): string {
  return Array.from({ length: len }, () => ALNUM[Math.floor(Math.random() * ALNUM.length)]).join("");
}

function requestId(): string {
  return `req_mock${Math.random().toString(36).slice(2, 10)}`;
}

function ok(body: unknown, status = 200): TransportResponse {
  return { status, body: structuredClone(body), headers: { "x-request-id": requestId() } };
}

function fail(status: number, code: string, message: string, details: Record<string, unknown> | null = null) {
  const rid = requestId();
  return {
    status,
    body: { error: { code, message, details, request_id: rid } },
    headers: { "x-request-id": rid },
  };
}

function summary(rows: LedgerRow[]) {
  const sameCurrency = rows.filter((r) => !r.captured_currency || r.captured_currency === r.currency);
  return {
    total_expected: sumMoney(rows.map((r) => r.expected_amount)),
    total_captured: sumMoney(sameCurrency.map((r) => r.captured_amount)),
    total_shortfall: sumMoney(rows.map((r) => r.shortfall_amount)),
    total_penalties: sumMoney(rows.map((r) => r.penalty_amount)),
    total_remediation_invoiced: sumMoney(rows.map((r) => r.remediation_invoice?.total_amount)),
  };
}

const PAYER_QR_PLACEHOLDER = "iVBORw0KGgoAAAANSUhEUgAAAMgAAADIAQMAAACXljzdAAAAA1BMVEX///+nxBvIAAAA";

function makeInvoice(r: LedgerRow): RemediationInvoiceRecord {
  const token = Array.from({ length: 16 }, () => "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"[Math.floor(Math.random() * 31)]).join("");
  const id = `INV2-${token.slice(0, 4)}-${token.slice(4, 8)}-${token.slice(8, 12)}-${token.slice(12, 16)}`;
  return {
    paypal_invoice_id: id,
    paypal_invoice_number: `FD-${(r.discrepancy_id ?? "dsc_X").replace("dsc_", "")}`,
    status: "SENT",
    total_amount: r.total_remediation_amount,
    currency: r.currency,
    recipient_email: r.client_paypal_email,
    payer_view_url: `https://www.sandbox.paypal.com/invoice/p/#${token}`,
    // PayPal's QR is sometimes unusable (Fallback B); the fixture value is a stub,
    // so the modal exercises the browser-drawn QR path in mock mode.
    qr_code_base64: PAYER_QR_PLACEHOLDER,
    sent_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    last_reminded_at: null,
  };
}

function addDecision(entry: Omit<DecisionLogEntry, "decision_id" | "created_at" | "policy_version">) {
  const d: DecisionLogEntry = {
    decision_id: `lsd_${String(state.decisions.length + 1).padStart(4, "0")}`,
    created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    policy_version: 2,
    ...entry,
  };
  state.decisions.unshift(d);
  return d;
}

// ------------------------------------------------------------------ Detective handlers

function extract(req: TransportRequest): TransportResponse {
  let name = "";
  let size = 0;
  let rawText = "";
  if (req.form) {
    const file = req.form.get("file");
    if (file instanceof File) {
      name = file.name;
      size = file.size;
    }
  } else if (req.body && typeof req.body === "object" && "raw_text" in req.body) {
    rawText = String((req.body as { raw_text: unknown }).raw_text ?? "");
  }
  if (!name && !rawText.trim()) {
    return fail(422, "VALIDATION_ERROR", "Send a contract file or paste the contract text.", {
      fields: { file: "required" },
    });
  }
  if (name && !/\.(pdf|md|txt)$/i.test(name)) {
    return fail(415, "UNSUPPORTED_FILE_TYPE", "Contracts must be PDF, Markdown or plain text.");
  }
  if (size > 5 * 1024 * 1024) {
    return fail(413, "FILE_TOO_LARGE", "Contracts can be at most 5 MB.");
  }
  const hay = `${name} ${rawText}`.toLowerCase();
  const stem = hay.includes("globex") ? "globex" : hay.includes("initech") ? "initech" : "acme";
  const ex = pick<ContractExtractResponse>(jsonFixtures, EXTRACT_BY_STEM[stem]);
  if (!ex) {
    return fail(422, "EXTRACTION_FAILED", "Mock fixtures are not available in this build. Type the terms in manually.");
  }
  const existing = state.contracts.get(ex.contract_id);
  state.contracts.set(ex.contract_id, {
    contract_id: ex.contract_id,
    status: "DRAFT",
    source_filename: name || ex.source_filename,
    extraction: ex.extraction,
    client_tags: existing?.client_tags ?? [],
  });
  return ok({ ...ex, status: "DRAFT", source_filename: name || "pasted-text.txt" }, 201);
}

function activate(contractId: string, req: TransportRequest): TransportResponse {
  const rec = state.contracts.get(contractId);
  if (!rec) return fail(404, "NOT_FOUND", `Contract ${contractId} was not found.`);
  const body = req.body as ContractActivateRequest | undefined;
  if (!body || !Array.isArray(body.milestones) || body.milestones.length === 0) {
    return fail(422, "VALIDATION_ERROR", "A contract needs at least one milestone.", {
      fields: { milestones: "required" },
    });
  }
  for (const m of body.milestones) {
    if (!/^\d+\.\d{2}$/.test(m.expected_amount)) {
      return fail(422, "VALIDATION_ERROR", `Milestone ${m.milestone_code}: amount must look like 1000.00.`, {
        fields: { expected_amount: m.expected_amount },
      });
    }
  }
  const milestones = body.milestones.map((m) => ({ ...m, milestone_id: m.milestone_id || nextId("ms_") }));
  rec.status = "ACTIVE";
  rec.extraction = { ...body, milestones, extraction_confidence: rec.extraction.extraction_confidence };

  for (const m of milestones) {
    const idx = state.rows.findIndex((r) => r.milestone_id === m.milestone_id);
    const base: LedgerRow = {
      milestone_id: m.milestone_id,
      contract_id: contractId,
      contract_title: body.contract_title,
      client_name: body.client_name,
      client_paypal_email: body.client_paypal_email,
      milestone_code: m.milestone_code,
      milestone_title: m.title,
      expected_amount: m.expected_amount,
      currency: body.currency,
      due_date: m.due_date,
      ...(unpaid() as Omit<
        LedgerRow,
        | "milestone_id"
        | "contract_id"
        | "contract_title"
        | "client_name"
        | "client_paypal_email"
        | "milestone_code"
        | "milestone_title"
        | "expected_amount"
        | "currency"
        | "due_date"
      >),
    };
    if (idx >= 0) {
      // Re-activating keeps payment history; only the terms change.
      const prev = state.rows[idx];
      state.rows[idx] = prev.captured_amount ? { ...prev, ...pickTerms(base) } : base;
    } else {
      state.rows.push(base);
    }
  }
  const res: ContractActivateResponse = { contract_id: contractId, status: "ACTIVE", milestones };
  return ok(res);
}

function pickTerms(r: LedgerRow): Partial<LedgerRow> {
  const { contract_title, client_name, client_paypal_email, milestone_code, milestone_title, expected_amount, due_date, currency } = r;
  return { contract_title, client_name, client_paypal_email, milestone_code, milestone_title, expected_amount, due_date, currency };
}

/** A small stand-in for P2's compliance_math: same rules the fixtures encode. */
function evaluate(row: LedgerRow, rec: ContractRecord, body: SimulatorPaymentRequest): DiscrepancyEvaluationResult {
  const terms = rec.extraction;
  const ms = terms.milestones.find((m) => m.milestone_id === row.milestone_id);
  const expected = toCents(row.expected_amount);
  const paid = toCents(body.simulated_amount);
  const daysLate = daysBetween(row.due_date, body.simulated_payment_date);
  const late = daysLate > terms.grace_period_days;
  const currencyMismatch = body.currency.toUpperCase() !== row.currency.toUpperCase();

  let type: DiscrepancyType;
  let shortfall = 0;
  let penalty = 0;
  if (currencyMismatch) {
    type = "CURRENCY_MISMATCH";
    shortfall = expected;
  } else {
    shortfall = Math.max(expected - paid, 0);
    const under = shortfall > 0;
    const pctBasisPoints = toCents(terms.late_fee_percentage); // "5.00" -> 500 = 5.00%
    if (late || (under && !terms.allow_partial_payments)) {
      penalty = Math.round((expected * pctBasisPoints) / 10_000);
    }
    type = under && late ? "UNDERPAID_AND_LATE" : under ? "UNDERPAID" : late ? "LATE_PAYMENT" : "COMPLIANT";
  }
  const total = shortfall + penalty;
  const money = (c: number) => formatMoney(centsToString(c), row.currency);
  const clause = type === "COMPLIANT" ? "" : terms.penalty_clause_quote || ms?.source_clause_quote || "";
  const parts: string[] = [];
  if (currencyMismatch) {
    parts.push(`Payment arrived in ${body.currency.toUpperCase()} but the contract settles in ${row.currency}; no FX is applied, so the full ${money(expected)} is still owed in ${row.currency}.`);
  } else if (type === "COMPLIANT") {
    parts.push(`Client paid ${money(paid)} for ${row.milestone_code} within terms. Nothing is owed.`);
  } else {
    parts.push(`Client paid ${money(paid)} against ${money(expected)} expected${shortfall ? ` (${money(shortfall)} shortfall)` : ""}`);
    if (late) parts.push(`${daysLate} days after the ${row.due_date} due date, beyond the ${terms.grace_period_days}-day grace period`);
    if (penalty) parts.push(`triggering a ${terms.late_fee_percentage}% (${money(penalty)}) late fee`);
  }
  const summaryText = parts.length > 1 ? `${parts.join(", ")}.` : parts[0];
  const lines: InvoiceLineItemSpec[] = [];
  if (shortfall) {
    lines.push({
      name: `Milestone shortfall - ${row.milestone_code}`,
      description: `Outstanding balance on ${row.milestone_code} ${row.milestone_title}.`,
      quantity: "1",
      unit_amount_value: centsToString(shortfall),
      currency_code: row.currency,
    });
  }
  if (penalty) {
    lines.push({
      name: `Late fee (${terms.late_fee_percentage}%) - ${row.milestone_code}`,
      description: "Contractual late fee on the full milestone balance.",
      quantity: "1",
      unit_amount_value: centsToString(penalty),
      currency_code: row.currency,
    });
  }
  return {
    discrepancy_type: type,
    discrepancy_status: "NONE",
    is_compliant: type === "COMPLIANT",
    expected_amount: row.expected_amount,
    captured_amount: centsToString(paid),
    captured_currency: body.currency.toUpperCase(),
    shortfall_amount: centsToString(shortfall),
    penalty_amount: centsToString(penalty),
    total_remediation_amount: centsToString(total),
    violated_clause_quote: clause,
    ai_audit_summary: summaryText,
    paypal_invoice_detail_note: summaryText,
    remediation_line_items: lines,
  };
}

/** Stand-in for P5's guard with the seeded policy (G-01, G-02, G-03). */
function decide(row: LedgerRow, rec: ContractRecord, ev: DiscrepancyEvaluationResult): Omit<GuardResult, "decision_id"> {
  const amount = toCents(ev.total_remediation_amount);
  const money = formatMoney(ev.total_remediation_amount, row.currency);
  if (rec.client_tags.includes("never_auto_contact")) {
    return { decision: "ASK", rule_ids: ["G-03"], reason: `${row.client_name} is tagged never_auto_contact, so Leash asks before sending.` };
  }
  if (ev.discrepancy_type === "CURRENCY_MISMATCH") {
    return { decision: "ASK", rule_ids: ["G-02"], reason: `Payment arrived in the wrong currency; Leash asks before billing ${money}.` };
  }
  const clauseOk = Boolean(ev.violated_clause_quote);
  const confident = rec.extraction.extraction_confidence >= 0.9;
  if (amount <= 25_000 && clauseOk && confident) {
    return { decision: "ALLOW", rule_ids: ["G-01"], reason: `${money} is within your $250.00 auto-send limit and the breached clause is quoted.` };
  }
  return { decision: "ASK", rule_ids: ["G-02"], reason: `${money} is over your $250.00 auto-send limit, so Leash is asking you first.` };
}

function simulate(req: TransportRequest): TransportResponse {
  const body = req.body as SimulatorPaymentRequest | undefined;
  if (!body?.milestone_id) return fail(422, "VALIDATION_ERROR", "Choose a milestone.", { fields: { milestone_id: "required" } });
  if (!/^\d+\.\d{2}$/.test(body.simulated_amount ?? "")) {
    return fail(422, "VALIDATION_ERROR", "Amount must look like 850.00.", { fields: { simulated_amount: body.simulated_amount } });
  }
  const idemKey = body.idempotency_key ? `sim:${body.idempotency_key}` : null;
  if (idemKey && state.idempotency.has(idemKey)) return ok(state.idempotency.get(idemKey));

  const row = state.rows.find((r) => r.milestone_id === body.milestone_id);
  if (!row) return fail(404, "NOT_FOUND", `Milestone ${body.milestone_id} was not found.`);
  const rec = state.contracts.get(row.contract_id);
  if (!rec || rec.status !== "ACTIVE") {
    return fail(409, "CONTRACT_NOT_ACTIVE", "Activate the contract before simulating a payment.");
  }

  const ev = evaluate(row, rec, body);
  const discrepancyId = ev.is_compliant ? null : nextId("dsc_");
  let leash: GuardResult | null = null;
  let invoice: RemediationInvoiceRecord | null = null;

  Object.assign(row, {
    captured_amount: ev.captured_amount,
    captured_currency: ev.captured_currency,
    shortfall_amount: ev.shortfall_amount,
    penalty_amount: ev.penalty_amount,
    total_remediation_amount: ev.total_remediation_amount,
    captured_at: `${body.simulated_payment_date}T14:22:10Z`,
    paypal_order_id: paypalId(),
    paypal_capture_id: paypalId(),
    webhook_verified: true,
    discrepancy_type: ev.discrepancy_type,
    discrepancy_status: "NONE",
    discrepancy_id: discrepancyId,
    violated_clause_quote: ev.violated_clause_quote || null,
    ai_audit_summary: ev.ai_audit_summary,
    remediation_invoice: null,
    leash_decision: null,
    remediation_sent_by: null,
  } satisfies Partial<LedgerRow>);

  if (discrepancyId) {
    const g = decide(row, rec, ev);
    const d = addDecision({
      action_type: "SEND_REMEDIATION_INVOICE",
      subject_type: "discrepancy",
      subject_id: discrepancyId,
      decision: g.decision,
      rule_ids: g.rule_ids,
      amount: ev.total_remediation_amount,
      reason: g.reason,
      actor: "leash",
    });
    leash = { ...g, decision_id: d.decision_id };
    row.leash_decision = g.decision;
    if (g.decision === "ALLOW") {
      invoice = makeInvoice(row);
      row.discrepancy_status = "REMEDIATED";
      row.remediation_invoice = invoice;
      row.remediation_sent_by = "leash-auto";
    } else {
      row.discrepancy_status = g.decision === "ASK" ? "PENDING_APPROVAL" : "BLOCKED";
    }
  }
  ev.discrepancy_status = row.discrepancy_status;

  const res: SimulatorPaymentResponse = {
    paypal_order_id: row.paypal_order_id ?? "",
    paypal_capture_id: row.paypal_capture_id ?? "",
    capture_status: "COMPLETED",
    discrepancy_id: discrepancyId,
    evaluation: ev,
    leash,
    remediation_invoice: invoice,
    payroll_run_id: row.milestone_id === "ms_01" ? "prn_0001" : null,
  };
  if (idemKey) state.idempotency.set(idemKey, res);
  return ok(res);
}

function remediate(discrepancyId: string, req: TransportRequest): TransportResponse {
  const body = req.body as RemediationRequest | undefined;
  if (!body?.confirm_send) return fail(422, "VALIDATION_ERROR", "confirm_send must be true.", { fields: { confirm_send: "required" } });
  const idemKey = body.idempotency_key ? `rem:${body.idempotency_key}` : null;
  if (idemKey && state.idempotency.has(idemKey)) return ok(state.idempotency.get(idemKey));

  const row = state.rows.find((r) => r.discrepancy_id === discrepancyId);
  if (!row) return fail(404, "NOT_FOUND", `Discrepancy ${discrepancyId} was not found.`);
  if (row.discrepancy_status === "BLOCKED") {
    const d = state.decisions.find((x) => x.subject_id === discrepancyId);
    return fail(409, "LEASH_DENIED", d?.reason ?? "Leash denied this invoice.", { decision_id: d?.decision_id ?? null });
  }
  if (row.discrepancy_status === "REMEDIATED") {
    return fail(409, "ALREADY_REMEDIATED", "This correction invoice was already sent.");
  }
  const invoice = makeInvoice(row);
  row.discrepancy_status = "REMEDIATED";
  row.remediation_invoice = invoice;
  row.remediation_sent_by = "owner";
  addDecision({
    action_type: "SEND_REMEDIATION_INVOICE",
    subject_type: "discrepancy",
    subject_id: discrepancyId,
    decision: "ALLOW",
    rule_ids: [],
    amount: row.total_remediation_amount,
    reason: "Owner approved and sent the correction invoice.",
    actor: "owner",
  });
  const res: RemediationResponse = { discrepancy_id: discrepancyId, discrepancy_status: "REMEDIATED", remediation_invoice: invoice };
  if (idemKey) state.idempotency.set(idemKey, res);
  return ok(res);
}

function margin(): TransportResponse {
  const rows: MarginRow[] = [...state.margin];
  // MS-01 follows the story: after the simulator captures $850, the run pays $420.
  const ms01 = state.rows.find((r) => r.milestone_id === "ms_01");
  if (ms01?.captured_amount) {
    const cashIn = ms01.captured_amount;
    const paid = "420.00";
    const m = toCents(cashIn) - toCents(paid);
    rows.unshift({
      milestone_id: "ms_01",
      milestone_code: "MS-01",
      cash_in: cashIn,
      invoiced_open: ms01.remediation_invoice?.status === "SENT" ? ms01.remediation_invoice.total_amount : "0.00",
      paid_to_team: paid,
      pending_to_team: "0.00",
      margin: centsToString(m),
      margin_pct: toCents(cashIn) ? centsToString(Math.round((m * 10_000) / toCents(cashIn))) : "0.00",
      payroll_status: "PAID",
    });
  }
  const res: MarginResponse = {
    rows,
    summary: {
      cash_in: sumMoney(rows.map((r) => r.cash_in)),
      paid_to_team: sumMoney(rows.map((r) => r.paid_to_team)),
      margin: sumMoney(rows.map((r) => r.margin)),
    },
  };
  return ok(res);
}

function decisions(req: TransportRequest): TransportResponse {
  let items = state.decisions;
  if (req.query.subject_id) items = items.filter((d) => d.subject_id === req.query.subject_id);
  if (req.query.subject_type) items = items.filter((d) => d.subject_type === req.query.subject_type);
  const res: DecisionLogResponse = { items, next_cursor: null };
  return ok(res);
}

// ------------------------------------------------------------------ Leash fixture routes (P5)

function leashFixture(name: string, status = 200): TransportResponse {
  const body = pick<unknown>(leashFixtures, name);
  return body === null ? fail(404, "NOT_FOUND", `Fixture ${name} is not available.`) : ok(body, status);
}

// ------------------------------------------------------------------ router

type Handler = (req: TransportRequest, params: string[]) => TransportResponse;

const routes: Array<[string, RegExp, Handler]> = [
  ["GET", /^\/health$/, () =>
    ok({ status: "ok", paypal_sandbox_connected: true, leash_enabled: true, ai_provider: "mock (fixtures)" })],
  ["POST", /^\/contracts\/extract$/, (r) => extract(r)],
  ["POST", /^\/contracts\/([^/]+)\/activate$/, (r, [id]) => activate(decodeURIComponent(id), r)],
  ["GET", /^\/ledger$/, () => ok({ summary: summary(state.rows), rows: state.rows } satisfies LedgerResponse)],
  ["POST", /^\/simulator\/pay$/, (r) => simulate(r)],
  ["POST", /^\/discrepancies\/([^/]+)\/remediate$/, (r, [id]) => remediate(decodeURIComponent(id), r)],
  ["GET", /^\/leash\/ledger\/margin$/, () => margin()],
  ["GET", /^\/leash\/decisions$/, (r) => decisions(r)],
  ["GET", /^\/leash\/policy$/, () => leashFixture("active_policy.json")],
  ["POST", /^\/leash\/policy\/compile$/, () => leashFixture("policy_compile_response.json")],
  ["POST", /^\/leash\/policy\/activate$/, (r) =>
    ok({ version: (r.body as { version?: number })?.version ?? 1, active: true, activated_at: new Date().toISOString() })],
  ["GET", /^\/leash\/team$/, () => {
    const omar = pick<unknown>(leashFixtures, "team_member_omar.json");
    return ok(omar ? [omar] : []);
  }],
  ["POST", /^\/leash\/team$/, (r) =>
    ok({ member_id: nextId("tm_"), email_changed_at: null, first_paid_at: null, active: true, ...(r.body as object) }, 201)],
  ["PATCH", /^\/leash\/team\/([^/]+)$/, (r, [id]) => {
    const omar = pick<Record<string, unknown>>(leashFixtures, "team_member_omar.json") ?? {};
    return ok({ ...omar, member_id: decodeURIComponent(id), ...(r.body as object) });
  }],
  ["POST", /^\/leash\/timesheets$/, () => leashFixture("timesheet_mapping_required.json", 201)],
  ["POST", /^\/leash\/timesheets\/([^/]+)\/mapping$/, () => leashFixture("timesheet_parsed.json")],
  ["POST", /^\/leash\/payroll\/runs$/, () => leashFixture("payroll_run_awaiting_approval.json", 201)],
  ["GET", /^\/leash\/payroll\/runs\/([^/]+)$/, () => leashFixture("payroll_run_awaiting_approval.json")],
  ["PATCH", /^\/leash\/payroll\/runs\/([^/]+)\/lines\/([^/]+)$/, () => leashFixture("payroll_run_after_exclude.json")],
  ["POST", /^\/leash\/payroll\/runs\/([^/]+)\/send$/, () => leashFixture("payroll_send_response.json", 202)],
  ["POST", /^\/demo\/reset$/, () => {
    resetMockState();
    return ok({ status: "reset" });
  }],
];

export const mockTransport: Transport = async (req) => {
  if (latencyMs > 0) await new Promise((r) => setTimeout(r, latencyMs));
  const isWrite = req.method !== "GET" && !req.path.startsWith("/webhooks/");
  if (isWrite && !req.headers["X-Demo-Key"]) {
    return fail(401, "DEMO_KEY_REQUIRED", "Unlock the demo with the owner key to make changes.");
  }
  for (const [method, re, handler] of routes) {
    if (method !== req.method) continue;
    const m = re.exec(req.path);
    if (m) {
      const res = handler(req, m.slice(1));
      if (isWrite && res.status < 300) persist();
      return res;
    }
  }
  return fail(404, "NOT_FOUND", `No mock for ${req.method} ${req.path}.`);
};


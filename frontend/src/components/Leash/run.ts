/**
 * Pure helpers for the payroll run review screen. No I/O.
 *
 * They mirror the server's gate (backend `leash/payroll/runs.py`): `set_line_action`
 * refuses APPROVED on a line whose flags carry DEFAULT (409 LINE_NOT_CHECKED), every edit
 * on a SENT/PAID/PARTIAL/FAILED run (409 RUN_NOT_SENDABLE), and `lines_ready_to_send`
 * refuses a send while an ASK line has no owner action or a run flag is not in
 * `approve_run_flags` (409 LEASH_ASK_UNRESOLVED). The UI only hides what the server
 * would refuse; the server stays the real guard.
 *
 * Money is integer cents (lib/money `toCents`), never floats.
 */
import { toCents } from "../../lib/money";
import type { PayrollLineOut, PayrollRunOut, PayrollRunStatus } from "../../types/contracts";

/** The guard's "could not check this" id. Nothing carrying it can be approved. */
export const UNCHECKED = "DEFAULT";

const LOCKED: ReadonlySet<PayrollRunStatus> = new Set(["SENT", "PAID", "PARTIAL", "FAILED"]);
const FINISHED: ReadonlySet<PayrollRunStatus> = new Set(["PAID", "PARTIAL", "FAILED"]);

/** Final statuses: polling stops here. */
export function isFinished(status: PayrollRunStatus): boolean {
  return FINISHED.has(status);
}

/** Sent or later: the server refuses every edit and every new send (RUN_NOT_SENDABLE). */
export function isLocked(status: PayrollRunStatus): boolean {
  return LOCKED.has(status);
}

/** Leash couldn't check this line yet. */
export function isUnchecked(line: PayrollLineOut): boolean {
  return line.flags.includes(UNCHECKED);
}

/** May the owner mark this line APPROVED? Never for DENY, DEFAULT or a sent run. */
export function canApprove(line: PayrollLineOut, status: PayrollRunStatus): boolean {
  return !isLocked(status) && line.decision !== "DENY" && !isUnchecked(line);
}

/** An ASK line nobody has decided yet. */
export function isOpenAsk(line: PayrollLineOut): boolean {
  return line.decision === "ASK" && line.owner_action === null;
}

export interface OpenAsks {
  lines: PayrollLineOut[];
  runFlags: string[];
}

/** Lines and run flags still waiting for the owner. `ticked` are the run flags approved on screen. */
export function openAsks(run: PayrollRunOut, ticked: readonly string[] = []): OpenAsks {
  return {
    lines: run.lines.filter(isOpenAsk),
    runFlags: run.run_decision === "ASK" ? run.run_flags.filter((f) => !ticked.includes(f)) : [],
  };
}

/** Run flags the owner can tick. DEFAULT is not approvable: the run must be re-checked. */
export function approvableRunFlags(run: PayrollRunOut): string[] {
  if (isLocked(run.status) || run.run_decision !== "ASK") return [];
  return run.run_flags.filter((f) => f !== UNCHECKED);
}

/** Will this line be in the PayPal batch? Same filter as the server's `lines_ready_to_send`. */
export function isPayable(line: PayrollLineOut): boolean {
  if (line.owner_action === "EXCLUDED" || toCents(line.gross) <= 0) return false;
  return line.decision === "ALLOW" || (line.decision === "ASK" && line.owner_action === "APPROVED");
}

export interface RunTotals {
  /** Sum of the lines that will be paid, in cents. */
  payCents: number;
  /** How many people will be paid. */
  people: number;
  /** Sum of the lines PayPal reported SUCCESS for, in cents. */
  paidCents: number;
  paidPeople: number;
  /** Lines that went out in the batch (any payout status). */
  sentPeople: number;
}

/** Exact run totals in integer cents. */
export function runTotalsCents(run: PayrollRunOut): RunTotals {
  const t: RunTotals = { payCents: 0, people: 0, paidCents: 0, paidPeople: 0, sentPeople: 0 };
  for (const line of run.lines) {
    if (isPayable(line)) {
      t.payCents += toCents(line.gross);
      t.people += 1;
    }
    if (line.payout_status !== null) t.sentPeople += 1;
    if (line.payout_status === "SUCCESS") {
      t.paidCents += toCents(line.gross);
      t.paidPeople += 1;
    }
  }
  return t;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** "1 person" / "2 people". */
export function peopleLabel(n: number): string {
  return plural(n, "person", "people");
}

/**
 * Why Pay team is disabled, in plain words; empty when it may be pressed.
 * `limitPct` is the policy's payroll margin limit, for the PR-06 wording.
 */
export function payBlockers(run: PayrollRunOut, ticked: readonly string[], limitPct = "80"): string[] {
  if (isLocked(run.status)) return [];
  if (run.run_decision === "DENY") return ["Leash denied this run"];
  const open = openAsks(run, ticked);
  const out: string[] = [];
  if (open.lines.length > 0) {
    out.push(`${plural(open.lines.length, "line still needs", "lines still need")} your decision`);
  }
  if (open.runFlags.includes(UNCHECKED)) out.push("Leash couldn't check this run yet. Refresh it");
  else if (open.runFlags.length === 1 && open.runFlags[0] === "PR-06") out.push(`Approve the ${limitPct}% check above`);
  else if (open.runFlags.length > 0) out.push("Approve the run checks above");
  if (out.length === 0 && runTotalsCents(run).people === 0) out.push("Nobody is left to pay");
  return out;
}

/** The 409 LEASH_ASK_UNRESOLVED details. The server lists `{line_id, flags}`; plain ids are accepted too. */
export function parseOpenAsks(details: Record<string, unknown> | null | undefined): { lineIds: string[]; runFlags: string[] } {
  const lineIds: string[] = [];
  const asks = details?.open_asks;
  if (Array.isArray(asks)) {
    for (const a of asks as unknown[]) {
      if (typeof a === "string") lineIds.push(a);
      else if (typeof a === "object" && a !== null && typeof (a as { line_id?: unknown }).line_id === "string") {
        lineIds.push((a as { line_id: string }).line_id);
      }
    }
  }
  const flags = details?.open_run_flags;
  const runFlags = Array.isArray(flags) ? (flags as unknown[]).filter((f): f is string => typeof f === "string") : [];
  return { lineIds, runFlags };
}

function nonce(): string {
  const bytes = new Uint8Array(8);
  try {
    crypto.getRandomValues(bytes);
  } catch {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** One per page session (module load), so a reload is a new session but a re-render is not. */
const sessionNonce = nonce();
const sendKeys = new Map<string, string>();

/**
 * One idempotency key per run for the page session: a double click, a retry after a lost
 * answer, or coming back to the page all reuse it, so the server returns the stored
 * batch instead of paying twice. At most 64 chars (server limit).
 */
export function sendKey(runId: string): string {
  let key = sendKeys.get(runId);
  if (!key) {
    key = `send-${runId}-${sessionNonce}`.slice(0, 64);
    sendKeys.set(runId, key);
  }
  return key;
}

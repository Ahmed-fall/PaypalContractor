/**
 * The three Leash calls Moamen's client does not expose yet (PR #16 extra routes).
 * They go through the exported `request`, so the demo key, the 401 unlock dialog and
 * mock mode work exactly as for every other call.
 */
import { request } from "../../api/client";
import type {
  ActiveLeashPolicy,
  LeashPolicy,
  LedgerResponse,
  PayrollLineUpdate,
  PayrollRunCreate,
  PayrollRunOut,
  PayrollSendRequest,
  PayrollSendResponse,
  PolicyActivateResponse,
  PolicyCompileResponse,
  TeamMemberCreate,
  TeamMemberOut,
  TeamMemberUpdate,
  TimesheetMappingConfirm,
  TimesheetParsed,
  TimesheetUploadResponse,
} from "../../types/contracts";

/** Add a team member. Quiet: the screen shows 409/422 errors beside the form. */
export const addMember = (body: TeamMemberCreate) => request<TeamMemberOut>("POST", "/leash/team", { body, quiet: true });

/** Edit a team member. Quiet for the same reason as `addMember`. */
export const patchMember = (memberId: string, body: TeamMemberUpdate) =>
  request<TeamMemberOut>("PATCH", `/leash/team/${encodeURIComponent(memberId)}`, { body, quiet: true });

/** The roster. Quiet: the Team screen shows its own load error with a Retry. */
export const listTeamQuiet = () => request<TeamMemberOut[]>("GET", "/leash/team", { quiet: true });

/** The latest 20 payroll runs, newest first. */
export const listPayrollRuns = () => request<PayrollRunOut[]>("GET", "/leash/payroll/runs", { quiet: true });

/** Plain-form path (Fallback E): validate and store a DRAFT policy. Nothing changes until it is activated. */
export const savePolicyDraft = (policy: LeashPolicy) =>
  request<PolicyCompileResponse>("POST", "/leash/policy", { body: policy, quiet: true });

/** Throw away a timesheet upload that is waiting for a column mapping. */
export const discardTimesheet = (uploadId: string) =>
  request<{ upload_id: string; status: "REJECTED" }>("POST", `/leash/timesheets/${encodeURIComponent(uploadId)}/discard`, {
    quiet: true,
  });

/** AI rule writer, quiet so a 501 (Fallback E) shows inline instead of as a toast. */
export const compilePolicyQuiet = (text: string) =>
  request<PolicyCompileResponse>("POST", "/leash/policy/compile", { body: { text }, quiet: true });

/** Make a saved draft the active policy. Quiet: 409s are shown on the Rules screen. */
export const activatePolicyQuiet = (version: number) =>
  request<PolicyActivateResponse>("POST", "/leash/policy/activate", { body: { version }, quiet: true });

/** Upload a timesheet. Quiet: 413/415/422 and duplicate uploads are shown beside the file picker. */
export const uploadTimesheetQuiet = (file: File) => {
  const form = new FormData();
  form.append("file", file);
  return request<TimesheetUploadResponse>("POST", "/leash/timesheets", { form, quiet: true });
};

/** Confirm which column is which. Quiet: 422 (missing field) and 409 are shown in the mapping dialog. */
export const confirmTimesheetMappingQuiet = (uploadId: string, body: TimesheetMappingConfirm) =>
  request<TimesheetParsed>("POST", `/leash/timesheets/${encodeURIComponent(uploadId)}/mapping`, { body, quiet: true });

/** Prepare a payroll run. Quiet: 404 and 409 MAPPING_REQUIRED are shown beside the button. */
export const createPayrollRunQuiet = (body: PayrollRunCreate) =>
  request<PayrollRunOut>("POST", "/leash/payroll/runs", { body, quiet: true });

/** The ledger, for the milestone list. Quiet: the card shows its own load error. */
export const ledgerQuiet = () => request<LedgerResponse>("GET", "/ledger", { quiet: true });

const runPath = (runId: string) => `/leash/payroll/runs/${encodeURIComponent(runId)}`;

/** One payroll run. Quiet: the run screen shows load and polling errors itself. */
export const payrollRunQuiet = (runId: string) => request<PayrollRunOut>("GET", runPath(runId), { quiet: true });

/** Approve or exclude one line; returns the whole re-checked run. Quiet: 409s show on the row. */
export const updatePayrollLineQuiet = (runId: string, lineId: string, body: PayrollLineUpdate) =>
  request<PayrollRunOut>("PATCH", `${runPath(runId)}/lines/${encodeURIComponent(lineId)}`, { body, quiet: true });

/**
 * Pay the team (one PayPal Payouts batch). `expectedTotal` is the total the owner confirmed;
 * the server answers 409 RUN_NOT_SENDABLE if the re-checked run would pay anything else.
 * Quiet: 409 open asks and 503 are shown beside Pay team.
 */
export const sendPayrollQuiet = (runId: string, body: PayrollSendRequest, expectedTotal: string) =>
  request<PayrollSendResponse>("POST", `${runPath(runId)}/send`, { body, query: { expected_total: expectedTotal }, quiet: true });

/** The active policy, for the payroll margin limit. Quiet: the screen falls back to 80%. */
export const activePolicyQuiet = () => request<ActiveLeashPolicy>("GET", "/leash/policy", { quiet: true });

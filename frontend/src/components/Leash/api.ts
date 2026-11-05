/**
 * The three Leash calls Moamen's client does not expose yet (PR #16 extra routes).
 * They go through the exported `request`, so the demo key, the 401 unlock dialog and
 * mock mode work exactly as for every other call.
 */
import { request } from "../../api/client";
import type {
  LeashPolicy,
  PayrollRunOut,
  PolicyActivateResponse,
  PolicyCompileResponse,
  TeamMemberCreate,
  TeamMemberOut,
  TeamMemberUpdate,
} from "../../types/contracts";

/** Add a team member. Quiet: the screen shows 409/422 errors beside the form. */
export const addMember = (body: TeamMemberCreate) => request<TeamMemberOut>("POST", "/leash/team", { body, quiet: true });

/** Edit a team member. Quiet for the same reason as `addMember`. */
export const patchMember = (memberId: string, body: TeamMemberUpdate) =>
  request<TeamMemberOut>("PATCH", `/leash/team/${encodeURIComponent(memberId)}`, { body, quiet: true });

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

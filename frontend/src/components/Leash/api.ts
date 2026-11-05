/**
 * The three Leash calls Moamen's client does not expose yet (PR #16 extra routes).
 * They go through the exported `request`, so the demo key, the 401 unlock dialog and
 * mock mode work exactly as for every other call.
 */
import { request } from "../../api/client";
import type { LeashPolicy, PayrollRunOut, PolicyCompileResponse } from "../../types/contracts";

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

/**
 * The only way the browser talks to the backend (plan A.7). Every call goes to
 * `${VITE_API_BASE_URL}/api/v1/*`; PayPal is never called from the browser and no
 * secret lives here. With VITE_USE_MOCKS=true the same functions are served from
 * shared/fixtures with 400 ms latency, including the Leash routes P5 builds on.
 */
import type {
  ActiveLeashPolicy,
  ContractActivateRequest,
  ContractActivateResponse,
  ContractExtractResponse,
  DecisionLogResponse,
  HealthResponse,
  LedgerResponse,
  MarginResponse,
  PayrollLineUpdate,
  PayrollRunCreate,
  PayrollRunOut,
  PayrollSendRequest,
  PayrollSendResponse,
  PolicyActivateRequest,
  PolicyActivateResponse,
  PolicyCompileRequest,
  PolicyCompileResponse,
  RemediationRequest,
  RemediationResponse,
  SimulatorPaymentRequest,
  SimulatorPaymentResponse,
  TeamMemberCreate,
  TeamMemberOut,
  TeamMemberUpdate,
  TimesheetMappingConfirm,
  TimesheetUploadResponse,
} from "../types/contracts";
import { getDemoKey } from "./demoKey";
import { ApiError, toApiError } from "./errors";
import { emit } from "../lib/events";

export { ApiError } from "./errors";

const env = import.meta.env;

export const apiConfig = {
  baseUrl: String(env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(/\/+$/, ""),
  useMocks: String(env.VITE_USE_MOCKS ?? "false").toLowerCase() === "true",
};

export type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

export interface RequestOptions {
  body?: unknown;
  form?: FormData;
  query?: Record<string, string | number | undefined | null>;
  /** Do not raise the global toast; the caller shows the error inline. */
  quiet?: boolean;
  signal?: AbortSignal;
}

/** What the mock layer (or a test) needs to answer a request. */
export interface TransportRequest {
  method: HttpMethod;
  path: string; // without /api/v1, e.g. "/ledger"
  query: Record<string, string>;
  body: unknown;
  form: FormData | null;
  headers: Record<string, string>;
}

export interface TransportResponse {
  status: number;
  body: unknown;
  headers?: Record<string, string>;
}

export type Transport = (req: TransportRequest) => Promise<TransportResponse>;

function isWrite(method: HttpMethod, path: string): boolean {
  return method !== "GET" && !path.startsWith("/webhooks/");
}

function buildQuery(query: RequestOptions["query"]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") out[k] = String(v);
  }
  return out;
}

const httpTransport: Transport = async (req) => {
  const qs = new URLSearchParams(req.query).toString();
  const url = `${apiConfig.baseUrl}/api/v1${req.path}${qs ? `?${qs}` : ""}`;
  const headers: Record<string, string> = { Accept: "application/json", ...req.headers };
  let body: BodyInit | undefined;
  if (req.form) {
    body = req.form; // browser sets the multipart boundary
  } else if (req.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(req.body);
  }
  let res: Response;
  try {
    res = await fetch(url, { method: req.method, headers, body });
  } catch {
    return { status: 0, body: null };
  }
  const text = await res.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  const requestId = res.headers.get("X-Request-Id");
  const outHeaders: Record<string, string> = {};
  if (requestId) outHeaders["x-request-id"] = requestId;
  return { status: res.status, body: parsed, headers: outHeaders };
};

let mockTransport: Transport | null = null;
let overrideTransport: Transport | null = null;

/** Tests and stories can swap the transport. Pass null to restore the default. */
export function setTransport(t: Transport | null): void {
  overrideTransport = t;
}

async function resolveTransport(): Promise<Transport> {
  if (overrideTransport) return overrideTransport;
  // Compile-time check: with VITE_USE_MOCKS unset the import below is dropped from the
  // bundle, so production never ships the mock layer or the fixtures.
  if (import.meta.env.VITE_USE_MOCKS !== "true" || !apiConfig.useMocks) return httpTransport;
  if (!mockTransport) {
    const mod = await import("./mocks");
    mockTransport = mod.mockTransport;
  }
  return mockTransport;
}

export async function request<T>(method: HttpMethod, path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (isWrite(method, path)) {
    const key = getDemoKey();
    if (key) headers["X-Demo-Key"] = key;
  }
  const transport = await resolveTransport();
  const res = await transport({
    method,
    path,
    query: buildQuery(opts.query),
    body: opts.body,
    form: opts.form ?? null,
    headers,
  });
  if (res.status >= 200 && res.status < 300) return res.body as T;

  const err = toApiError(res.status, res.body, res.headers?.["x-request-id"] ?? null);
  if (err.status === 401 || err.code === "DEMO_KEY_REQUIRED") emit("unlock-required", err);
  else if (!opts.quiet) emit("api-error", err);
  throw err;
}

// ---------------------------------------------------------------- Detective (P3)

export const api = {
  health: () => request<HealthResponse>("GET", "/health", { quiet: true }),

  extractContract: (input: { file: File } | { raw_text: string }) => {
    if ("file" in input) {
      const form = new FormData();
      form.append("file", input.file);
      return request<ContractExtractResponse>("POST", "/contracts/extract", { form, quiet: true });
    }
    return request<ContractExtractResponse>("POST", "/contracts/extract", {
      body: { raw_text: input.raw_text },
      quiet: true,
    });
  },

  activateContract: (contractId: string, body: ContractActivateRequest) =>
    request<ContractActivateResponse>("POST", `/contracts/${encodeURIComponent(contractId)}/activate`, { body }),

  ledger: () => request<LedgerResponse>("GET", "/ledger"),

  simulatePay: (body: SimulatorPaymentRequest) =>
    request<SimulatorPaymentResponse>("POST", "/simulator/pay", { body }),

  remediate: (discrepancyId: string, body: RemediationRequest) =>
    request<RemediationResponse>("POST", `/discrepancies/${encodeURIComponent(discrepancyId)}/remediate`, {
      body,
      quiet: true,
    }),

  // ------------------------------------------------------------ Leash (P5 screens)
  // Exposed here so P5's pages share one client, one error path and mock mode.

  margin: () => request<MarginResponse>("GET", "/leash/ledger/margin", { quiet: true }),
  decisions: (query: { subject_id?: string; subject_type?: string; cursor?: string; limit?: number } = {}) =>
    request<DecisionLogResponse>("GET", "/leash/decisions", { query, quiet: true }),
  activePolicy: () => request<ActiveLeashPolicy>("GET", "/leash/policy"),
  compilePolicy: (body: PolicyCompileRequest) =>
    request<PolicyCompileResponse>("POST", "/leash/policy/compile", { body }),
  activatePolicy: (body: PolicyActivateRequest) =>
    request<PolicyActivateResponse>("POST", "/leash/policy/activate", { body }),
  team: () => request<TeamMemberOut[]>("GET", "/leash/team"),
  addTeamMember: (body: TeamMemberCreate) => request<TeamMemberOut>("POST", "/leash/team", { body }),
  updateTeamMember: (memberId: string, body: TeamMemberUpdate) =>
    request<TeamMemberOut>("PATCH", `/leash/team/${encodeURIComponent(memberId)}`, { body }),
  uploadTimesheet: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<TimesheetUploadResponse>("POST", "/leash/timesheets", { form });
  },
  confirmTimesheetMapping: (uploadId: string, body: TimesheetMappingConfirm) =>
    request<TimesheetUploadResponse>("POST", `/leash/timesheets/${encodeURIComponent(uploadId)}/mapping`, { body }),
  createPayrollRun: (body: PayrollRunCreate) =>
    request<PayrollRunOut>("POST", "/leash/payroll/runs", { body }),
  payrollRun: (runId: string) => request<PayrollRunOut>("GET", `/leash/payroll/runs/${encodeURIComponent(runId)}`),
  updatePayrollLine: (runId: string, lineId: string, body: PayrollLineUpdate) =>
    request<PayrollRunOut>(
      "PATCH",
      `/leash/payroll/runs/${encodeURIComponent(runId)}/lines/${encodeURIComponent(lineId)}`,
      { body },
    ),
  sendPayroll: (runId: string, body: PayrollSendRequest) =>
    request<PayrollSendResponse>("POST", `/leash/payroll/runs/${encodeURIComponent(runId)}/send`, { body }),

  resetDemo: () => request<{ status: string }>("POST", "/demo/reset"),
};

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

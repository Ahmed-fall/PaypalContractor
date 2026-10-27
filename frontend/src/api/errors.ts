/** Mirrors the single error body every non-2xx response uses (plan A.4). */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown> | null;
    request_id?: string | null;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | null;
  readonly requestId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> | null = null,
    requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

function isErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const err = (value as { error: unknown }).error;
  return (
    typeof err === "object" &&
    err !== null &&
    typeof (err as { code?: unknown }).code === "string" &&
    typeof (err as { message?: unknown }).message === "string"
  );
}

/** Build an ApiError from any response body, including non-A.4 shapes (FastAPI 422s, proxies). */
export function toApiError(status: number, body: unknown, headerRequestId: string | null): ApiError {
  if (isErrorBody(body)) {
    return new ApiError(
      status,
      body.error.code,
      body.error.message,
      body.error.details ?? null,
      body.error.request_id ?? headerRequestId,
    );
  }
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    const message = typeof detail === "string" ? detail : "The request was not valid.";
    return new ApiError(status, status === 422 ? "VALIDATION_ERROR" : "HTTP_ERROR", message, null, headerRequestId);
  }
  const fallback =
    status === 0
      ? "Could not reach the server. Check that the backend is running."
      : `Request failed with status ${status}.`;
  return new ApiError(status, status === 0 ? "NETWORK_ERROR" : "INTERNAL_ERROR", fallback, null, headerRequestId);
}

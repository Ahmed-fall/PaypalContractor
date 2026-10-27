import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { on } from "../../lib/events";
import type { ApiError } from "../../api/errors";

type Tone = "error" | "success" | "info";

interface ToastItem {
  id: number;
  tone: Tone;
  message: string;
  requestId?: string | null;
}

interface ToastApi {
  show: (message: string, tone?: Tone, requestId?: string | null) => void;
  error: (err: ApiError | Error | string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

/** The single toast (plan A.7): shows error.message with request_id in small text. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const dismiss = useCallback((id: number) => setItems((xs) => xs.filter((x) => x.id !== id)), []);

  const show = useCallback(
    (message: string, tone: Tone = "info", requestId: string | null = null) => {
      const id = ++seq.current;
      setItems((xs) => [...xs.slice(-3), { id, tone, message, requestId }]);
      window.setTimeout(() => dismiss(id), tone === "error" ? 8000 : 4500);
    },
    [dismiss],
  );

  const error = useCallback(
    (err: ApiError | Error | string) => {
      if (typeof err === "string") return show(err, "error");
      const rid = "requestId" in err ? (err as ApiError).requestId : null;
      show(err.message, "error", rid);
    },
    [show],
  );

  useEffect(() => on("api-error", (e) => error(e)), [error]);

  const value = useMemo(() => ({ show, error }), [show, error]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(92vw,380px)] flex-col gap-2" aria-live="polite">
        {items.map((t) => (
          <div
            key={t.id}
            role={t.tone === "error" ? "alert" : "status"}
            className={`pointer-events-auto rounded border border-l-[3px] bg-white px-4 py-3 text-[14px] shadow-pop ${
              t.tone === "error" ? "border-paper-line border-l-bad" : t.tone === "success" ? "border-paper-line border-l-good" : "border-paper-line border-l-ink"
            }`}
          >
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-ink">{t.message}</p>
                {t.requestId && <p className="num mt-1 text-[12px] text-ink-muted">Request {t.requestId}</p>}
              </div>
              <button
                type="button"
                onClick={() => dismiss(t.id)}
                className="text-ink-muted hover:text-ink"
                aria-label="Dismiss notification"
              >
                ×
              </button>
            </div>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return ctx;
}

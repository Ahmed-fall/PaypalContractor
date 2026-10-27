import { useRef, useState, type DragEvent } from "react";
import { Button, Spinner } from "../common/Button";
import { apiConfig } from "../../api/client";

export interface UploadInput {
  file?: File;
  rawText?: string;
}

interface Props {
  busy: boolean;
  error: string | null;
  onSubmit: (input: UploadInput) => void;
  samples?: { label: string; load: () => File | null }[];
}

const ACCEPT = ".pdf,.md,.txt,application/pdf,text/markdown,text/plain";
const MAX_BYTES = 5 * 1024 * 1024;

/** Drag-and-drop contract upload (PDF, MD or TXT, max 5 MB) or pasted text. */
export function UploadPanel({ busy, error, onSubmit, samples = [] }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [mode, setMode] = useState<"file" | "text">("file");
  const [text, setText] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  const accept = (file: File | undefined | null) => {
    if (!file) return;
    if (!/\.(pdf|md|txt)$/i.test(file.name)) {
      setLocalError("Contracts must be a PDF, Markdown (.md) or plain text (.txt) file.");
      return;
    }
    if (file.size > MAX_BYTES) {
      setLocalError("That file is over 5 MB. Upload a smaller copy or paste the text.");
      return;
    }
    setLocalError(null);
    onSubmit({ file });
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDrag(false);
    if (busy) return;
    accept(e.dataTransfer.files?.[0]);
  };

  const shown = localError ?? error;

  return (
    <div className="rounded-xl border border-paper-line bg-white p-5 shadow-card sm:p-6">
      <div className="mb-4 flex gap-1 rounded-lg bg-paper p-1 text-sm" role="tablist">
        {(["file", "text"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
            onClick={() => setMode(m)}
            className={`flex-1 rounded-md px-3 py-1.5 font-medium transition ${mode === m ? "bg-white text-ink shadow-card" : "text-ink-muted hover:text-ink"}`}
          >
            {m === "file" ? "Upload file" : "Paste text"}
          </button>
        ))}
      </div>

      {mode === "file" ? (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            if (!busy) setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
          onClick={() => !busy && input.current?.click()}
          onKeyDown={(e) => {
            if ((e.key === "Enter" || e.key === " ") && !busy) input.current?.click();
          }}
          role="button"
          tabIndex={0}
          aria-label="Upload a contract"
          className={`flex min-h-[220px] cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-6 text-center transition ${
            drag ? "border-brand bg-brand-soft" : "border-slate-300 hover:border-slate-400 hover:bg-paper/60"
          } ${busy ? "pointer-events-none opacity-70" : ""}`}
        >
          {busy ? (
            <div className="flex flex-col items-center gap-3 text-ink-soft">
              <Spinner className="h-6 w-6 text-brand" />
              <p className="text-sm font-medium">Reading the contract and extracting payment rules…</p>
            </div>
          ) : (
            <>
              <svg aria-hidden viewBox="0 0 24 24" className="h-10 w-10 text-slate-400" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
                <path d="M14 3v5h5M12 18v-6M9 15l3-3 3 3" />
              </svg>
              <p className="mt-3 text-sm font-medium text-ink">Drop a signed contract here, or click to choose</p>
              <p className="mt-1 text-xs text-ink-muted">PDF, Markdown or text, up to 5 MB</p>
            </>
          )}
          <input
            ref={input}
            type="file"
            accept={ACCEPT}
            className="hidden"
            onChange={(e) => {
              accept(e.target.files?.[0]);
              e.target.value = "";
            }}
          />
        </div>
      ) : (
        <div className="space-y-3">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={9}
            placeholder="Paste the contract text…"
            className="w-full rounded-lg border border-paper-line p-3 font-mono text-xs outline-none focus:border-brand focus:ring-2 focus:ring-brand/20"
          />
          <div className="flex justify-end">
            <Button disabled={busy || !text.trim()} onClick={() => onSubmit({ rawText: text })}>
              {busy && <Spinner />} Extract rules
            </Button>
          </div>
        </div>
      )}

      {shown && (
        <p role="alert" className="mt-4 rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">
          {shown}
        </p>
      )}

      {samples.length > 0 && (
        <div className="mt-5 border-t border-paper-line pt-4">
          <p className="text-xs font-medium text-ink-muted">
            {apiConfig.useMocks ? "Sample contracts (mock mode)" : "Sample contracts"}
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            {samples.map((s) => (
              <Button
                key={s.label}
                size="sm"
                variant="secondary"
                disabled={busy}
                onClick={() => {
                  const f = s.load();
                  if (f) accept(f);
                }}
              >
                {s.label}
              </Button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

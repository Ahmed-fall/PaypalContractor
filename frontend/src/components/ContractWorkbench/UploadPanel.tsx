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
    <div className="rounded-lg border border-paper-line bg-white p-5 sm:p-7">
      <div className="mb-5 flex gap-6 border-b border-paper-line text-[14px]" role="tablist">
        {(["file", "text"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
            onClick={() => setMode(m)}
            className={`-mb-px border-b-2 pb-2 transition-colors ${mode === m ? "border-ink font-medium text-ink" : "border-transparent text-ink-muted hover:text-ink"}`}
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
            drag ? "border-ink bg-paper" : "border-paper-line hover:border-ink-muted"
          } ${busy ? "pointer-events-none opacity-70" : ""}`}
        >
          {busy ? (
            <div className="flex flex-col items-center gap-3 text-ink-soft">
              <Spinner className="h-6 w-6 text-ink" />
              <p className="text-[14px] font-medium">Reading the payment clauses…</p>
            </div>
          ) : (
            <>
              <svg aria-hidden viewBox="0 0 24 24" className="h-9 w-9 text-ink-muted" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
                <path d="M14 3v5h5M12 18v-6M9 15l3-3 3 3" />
              </svg>
              <p className="mt-3 text-[15px] font-medium text-ink">Drop the signed contract here, or choose a file</p>
              <p className="mt-1 text-[13px] text-ink-muted">PDF, Markdown or plain text, up to 5 MB</p>
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
            className="w-full rounded border border-paper-line p-3 text-[13px] leading-relaxed outline-none focus:border-ink"
          />
          <div className="flex justify-end">
            <Button disabled={busy || !text.trim()} onClick={() => onSubmit({ rawText: text })}>
              {busy && <Spinner />} Extract rules
            </Button>
          </div>
        </div>
      )}

      {shown && (
        <p role="alert" className="mt-4 border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
          {shown}
        </p>
      )}

      {samples.length > 0 && (
        <div className="mt-5 border-t border-paper-line pt-4">
          <p className="text-[13px] text-ink-muted">{apiConfig.useMocks ? "Or try one of the sample contracts" : "Sample contracts"}</p>
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

import { useEffect, useMemo, useState } from "react";
import { sectionLabel } from "./validation";

interface Props {
  filename: string;
  file: File | null; // only while the upload is in memory (PDF preview)
  text: string | null;
  milestoneQuotes: string[];
  penaltyQuote: string;
}

/** Strip light Markdown so the contract reads like a document. */
function plain(s: string): string {
  return s
    .replace(/^#{1,6}\s*/gm, "")
    .replace(/\*\*([\s\S]*?)\*\*/g, "$1")
    .replace(/__([\s\S]*?)__/g, "$1");
}

/**
 * Left half of the split view: the source document. Text contracts render with the
 * cited sections highlighted (milestones in blue, penalty in amber). PDFs render in
 * the browser's viewer while the file is in memory.
 */
export function SourcePane({ filename, file, text, milestoneQuotes, penaltyQuote }: Props) {
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!file || !/\.pdf$/i.test(file.name)) {
      setPdfUrl(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPdfUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const marks = useMemo(() => {
    const map = new Map<string, "milestone" | "penalty">();
    milestoneQuotes.forEach((q) => {
      const l = sectionLabel(q);
      if (l) map.set(l.toLowerCase(), "milestone");
    });
    const p = sectionLabel(penaltyQuote);
    if (p) map.set(p.toLowerCase(), "penalty");
    return map;
  }, [milestoneQuotes, penaltyQuote]);

  const paragraphs = useMemo(() => (text ? plain(text).split(/\n\s*\n/) : []), [text]);

  return (
    <div className="flex h-full min-h-[420px] flex-col overflow-hidden rounded-lg border border-paper-line bg-white">
      <div className="flex items-center justify-between gap-3 border-b border-paper-line px-4 py-2.5">
        <p className="truncate text-[14px] font-medium text-ink" title={filename}>
          {filename}
        </p>
        <p className="shrink-0 text-[12.5px] text-ink-muted">
          <span className="hl-soft text-ink">Highlighted</span> clauses became rules
        </p>
      </div>
      {pdfUrl ? (
        <iframe title="Contract PDF" src={pdfUrl} className="min-h-[560px] w-full flex-1" />
      ) : paragraphs.length ? (
        <article className="flex-1 space-y-3 overflow-y-auto px-6 py-5 text-[14px] leading-[1.65] text-ink-soft">
          {paragraphs.map((p, i) => {
            const label = /Section\s+\d+(?:\.\d+)*/i.exec(p)?.[0]?.replace(/\s+/g, " ").toLowerCase();
            const kind = label ? marks.get(label) : undefined;
            const isHeading = p.length < 70 && /^\d+\.\s/.test(p.trim());
            return (
              <p
                key={i}
                className={
                  kind
                    ? "text-ink"
                    : isHeading
                        ? "pt-2 font-semibold text-ink"
                        : i === 0
                          ? "text-base font-semibold text-ink"
                          : ""
                }
              >
                {kind ? <span className="hl-soft">{p}</span> : p}
              </p>
            );
          })}
        </article>
      ) : (
        <div className="flex flex-1 flex-col justify-center gap-3 px-6 py-6 text-[14px] text-ink-muted">
          <p>The original file is not kept in the browser after a refresh. These are the clauses the AI cited:</p>
          {[...milestoneQuotes, penaltyQuote].filter(Boolean).map((q, i) => (
            <p key={i} className="text-ink">
              <span className="hl-soft">{q}</span>
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

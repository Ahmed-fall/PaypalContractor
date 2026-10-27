import { useEffect, useRef, type ReactNode } from "react";

interface ModalProps {
  open: boolean;
  title: string;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: "sm" | "md" | "lg";
}

/** Accessible modal shell: Esc closes, focus moves inside, backdrop click closes. */
export function Modal({ open, title, subtitle, onClose, children, footer, width = "md" }: ModalProps) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const t = window.setTimeout(() => {
      const first = panel.current?.querySelector<HTMLElement>("[data-autofocus], input, button, [tabindex]");
      first?.focus();
    }, 0);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(t);
      document.removeEventListener("keydown", onKey);
      prev?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;
  const w = width === "sm" ? "max-w-md" : width === "lg" ? "max-w-3xl" : "max-w-xl";
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-[#16302F]/45 p-0 sm:items-center sm:p-6" onMouseDown={onClose}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`flex max-h-[92vh] w-full ${w} flex-col overflow-hidden rounded-t-lg bg-white shadow-pop sm:rounded-lg`}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-4 border-b border-paper-line px-7 py-5">
          <div className="min-w-0">
            <h2 className="text-[18px] font-semibold tracking-[-0.01em] text-ink">{title}</h2>
            {subtitle && <div className="mt-1 text-[14px] text-ink-muted">{subtitle}</div>}
          </div>
          <button type="button" onClick={onClose} className="rounded p-1 text-xl leading-none text-ink-muted hover:bg-paper hover:text-ink" aria-label="Close">
            ×
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-7 py-6">{children}</div>
        {footer && <footer className="flex flex-wrap items-center justify-end gap-3 border-t border-paper-line px-7 py-4">{footer}</footer>}
      </div>
    </div>
  );
}

import { useEffect, useId, useLayoutEffect, useRef, type ReactNode } from "react";

interface ModalProps {
  open: boolean;
  title: string;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: "sm" | "md" | "lg";
}

const FOCUSABLE = [
  "a[href]",
  "area[href]",
  "button",
  "input:not([type='hidden'])",
  "select",
  "textarea",
  "iframe",
  "[contenteditable]:not([contenteditable='false'])",
  "[tabindex]",
].join(",");

/** Elements Tab can reach inside `root`, in document order. */
function tabbables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.tabIndex >= 0 && !el.matches(":disabled") && !el.closest("[hidden], [inert]"),
  );
}

/*
 * Background isolation, shared by every open Modal. Modals render in place (no portal), so
 * "the background" is every sibling of every ancestor of the topmost modal, up to <body>.
 * Those get `inert` + `aria-hidden`; only the topmost modal stays interactive, and closing it
 * hands isolation to the one below. Live regions (the toast stack) stay outside the inert set
 * so errors raised while a dialog is open are still announced.
 */
const stack: HTMLElement[] = [];
const touched = new Map<Element, { inert: boolean; ariaHidden: string | null }>();
const SKIP = new Set(["SCRIPT", "STYLE", "TEMPLATE", "LINK", "META", "NOSCRIPT"]);

function hide(el: Element) {
  if (SKIP.has(el.tagName) || el.hasAttribute("aria-live")) return;
  if (el.querySelector("[aria-live]")) {
    Array.from(el.children).forEach(hide);
    return;
  }
  if (!touched.has(el)) touched.set(el, { inert: el.hasAttribute("inert"), ariaHidden: el.getAttribute("aria-hidden") });
  el.setAttribute("inert", "");
  el.setAttribute("aria-hidden", "true");
}

function syncBackground() {
  touched.forEach((was, el) => {
    if (!was.inert) el.removeAttribute("inert");
    if (was.ariaHidden === null) el.removeAttribute("aria-hidden");
    else el.setAttribute("aria-hidden", was.ariaHidden);
  });
  touched.clear();
  const top = stack[stack.length - 1];
  if (!top) return;
  for (let node: Element = top; node.parentElement && node !== document.body; node = node.parentElement) {
    for (const sibling of Array.from(node.parentElement.children)) if (sibling !== node) hide(sibling);
  }
}

/**
 * Accessible modal shell. While open: the rest of the page is inert, Tab / Shift+Tab cycle
 * inside the dialog, focus starts on `[data-autofocus]` (or an `autoFocus` child, else the
 * first focusable element) and returns to the opener on close. Esc and a backdrop click close.
 */
export function Modal({ open, title, subtitle, onClose, children, footer, width = "md" }: ModalProps) {
  const root = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  const id = useId();
  const titleId = `${id}-title`;
  const descId = `${id}-desc`;

  // Callers often pass an inline onClose; keep it out of the effect deps so a parent
  // re-render never re-runs open/close (which would yank focus back to the opener).
  useLayoutEffect(() => {
    closeRef.current = onClose;
  });

  useEffect(() => {
    const backdrop = root.current;
    const box = panel.current;
    if (!open || !backdrop || !box) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    stack.push(backdrop);
    syncBackground();

    const t = window.setTimeout(() => {
      if (box.contains(document.activeElement)) return; // a child's autoFocus already won
      const preferred = box.querySelector<HTMLElement>("[data-autofocus]");
      const target = preferred && tabbables(box).includes(preferred) ? preferred : (tabbables(box)[0] ?? box);
      target.focus();
    }, 0);

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeRef.current();
        return;
      }
      if (e.key !== "Tab" || stack[stack.length - 1] !== backdrop) return;
      const items = tabbables(box);
      const active = document.activeElement;
      if (items.length === 0) {
        e.preventDefault();
        box.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const inside = active instanceof Node && box.contains(active);
      if (e.shiftKey) {
        if (!inside || active === first || active === box) {
          e.preventDefault();
          last.focus();
        }
      } else if (!inside || active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);

    return () => {
      window.clearTimeout(t);
      document.removeEventListener("keydown", onKey);
      const i = stack.lastIndexOf(backdrop);
      const wasTop = i === stack.length - 1;
      if (i >= 0) stack.splice(i, 1);
      syncBackground(); // un-inert first: an inert opener can't take focus
      if (wasTop && opener?.isConnected) opener.focus();
    };
  }, [open]);

  if (!open) return null;
  const w = width === "sm" ? "max-w-md" : width === "lg" ? "max-w-3xl" : "max-w-xl";
  return (
    <div
      ref={root}
      className="fixed inset-0 z-50 flex items-end justify-center bg-[#16302F]/45 p-0 sm:items-center sm:p-6"
      onMouseDown={onClose}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={subtitle ? descId : undefined}
        tabIndex={-1}
        className={`flex max-h-[92vh] w-full ${w} flex-col overflow-hidden rounded-t-lg bg-white shadow-pop outline-none sm:rounded-lg`}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-4 border-b border-paper-line px-7 py-5">
          <div className="min-w-0">
            <h2 id={titleId} className="text-[18px] font-semibold tracking-[-0.01em] text-ink">
              {title}
            </h2>
            {subtitle && (
              <div id={descId} className="mt-1 text-[14px] text-ink-muted">
                {subtitle}
              </div>
            )}
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

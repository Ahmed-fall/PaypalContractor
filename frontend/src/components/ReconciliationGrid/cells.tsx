import type { CustomCellRendererProps } from "ag-grid-react";
import type { GridRow, TotalsRow } from "./model";
import { ComplianceBadge, LifecycleBadge, PayrollBadge } from "../common/StatusBadge";
import { formatMoney, toCents } from "../../lib/money";
import { formatDate, formatDateTime } from "../../lib/dates";

export type Row = GridRow | TotalsRow;
export const isTotals = (r: Row | undefined): r is TotalsRow => Boolean(r && "__totals" in r);

export interface GridContext {
  expanded: Set<string>;
  toggle: (milestoneId: string) => void;
  openRemediation: (discrepancyId: string) => void;
}

type P = CustomCellRendererProps<Row, unknown, GridContext>;

export function ExpandCell({ data, context }: P) {
  if (!data || isTotals(data) || data.__detailFor) return null;
  const open = context.expanded.has(data.milestone_id);
  return (
    <button
      type="button"
      onClick={() => context.toggle(data.milestone_id)}
      aria-expanded={open}
      aria-label={open ? `Hide details for ${data.milestone_code}` : `Show details for ${data.milestone_code}`}
      className="flex h-7 w-7 items-center justify-center rounded text-ink-muted hover:bg-paper hover:text-ink"
    >
      <svg viewBox="0 0 20 20" className={`h-4 w-4 transition-transform ${open ? "rotate-90" : ""}`} fill="currentColor" aria-hidden>
        <path d="M7.2 4.2a1 1 0 0 1 1.4 0l5.1 5.1a1 1 0 0 1 0 1.4l-5.1 5.1a1 1 0 1 1-1.4-1.4L11.6 10 7.2 5.6a1 1 0 0 1 0-1.4z" />
      </svg>
    </button>
  );
}

export function MilestoneCell({ data }: P) {
  if (!data) return null;
  if (isTotals(data)) return <span className="text-[13px] font-semibold text-ink">{data.milestone_code}</span>;
  return (
    <div className="flex flex-col justify-center leading-tight">
      <span className="text-[13.5px] font-semibold text-ink">{data.milestone_code}</span>
      <span className="truncate text-[12.5px] text-ink-muted" title={data.milestone_title}>
        {data.milestone_title}
      </span>
    </div>
  );
}

export function ClientCell({ data }: P) {
  if (!data || isTotals(data)) return null;
  return (
    <div className="flex flex-col justify-center leading-tight">
      <span className="truncate text-ink">{data.client_name}</span>
      <span className="truncate text-[12.5px] text-ink-muted" title={data.contract_title}>
        {data.contract_title}
      </span>
    </div>
  );
}

export function DueCell({ data }: P) {
  if (!data || isTotals(data)) return null;
  return <span className="num text-ink-soft">{formatDate(data.due_date)}</span>;
}

export function moneyCell(field: keyof GridRow & keyof TotalsRow, opts: { emphasise?: boolean; negativeTone?: boolean } = {}) {
  return function Money({ data }: P) {
    if (!data) return null;
    const v = data[field] as string | null;
    if (!isTotals(data) && field === "captured_amount" && !data.captured_amount) {
      return <span className="text-[13px] text-ink-muted">Nothing yet</span>;
    }
    const cents = toCents(v);
    const tone = opts.negativeTone && cents > 0 ? "text-bad" : cents === 0 ? "text-ink-muted" : "text-ink";
    const mismatch = !isTotals(data) && field === "captured_amount" && data.captured_currency && data.captured_currency !== data.currency;
    return (
      <span className={`num ${tone} ${opts.emphasise ? "font-semibold" : ""}`}>
        {mismatch ? (
          <span title={`Arrived in ${data.captured_currency}; excluded from totals`}>
            {data.captured_amount} <span className="text-[11px] font-semibold text-warn">{data.captured_currency}</span>
          </span>
        ) : (
          formatMoney(v, data.currency)
        )}
      </span>
    );
  };
}

export function CaptureCell({ data }: P) {
  if (!data || isTotals(data)) return null;
  if (!data.paypal_capture_id) return <span className="text-[13px] text-ink-muted">None</span>;
  return (
    <span className="flex items-center gap-1.5" title={data.webhook_verified ? "Confirmed by a verified PayPal webhook" : "Awaiting webhook verification"}>
      {data.webhook_verified ? (
        <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0 text-good" fill="currentColor" aria-label="Verified">
          <path d="M10 1.5 3 4.3v5.2c0 4.3 3 7.9 7 9 4-1.1 7-4.7 7-9V4.3zm3.7 6.2-4.4 4.6a.8.8 0 0 1-1.1 0L6.3 10.4a.8.8 0 1 1 1.1-1.1l1.3 1.3 3.9-4a.8.8 0 0 1 1.1 1.1z" />
        </svg>
      ) : (
        <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0 text-slate-400" fill="none" stroke="currentColor" strokeWidth="1.6" aria-label="Unverified">
          <path d="M10 2 3.5 4.6v4.9c0 4 2.8 7.4 6.5 8.4 3.7-1 6.5-4.4 6.5-8.4V4.6z" />
        </svg>
      )}
      <span className="num truncate text-[13px] text-ink-soft">{data.paypal_capture_id}</span>
    </span>
  );
}

export function StatusCell({ data }: P) {
  if (!data || isTotals(data)) return null;
  return (
    <div className="flex h-full flex-col justify-center gap-0.5 leading-tight">
      <ComplianceBadge type={data.discrepancy_type} />
      <LifecycleBadge status={data.discrepancy_status} sentBy={data.remediation_sent_by} decision={data.leash_decision} />
    </div>
  );
}

export function MarginCell({ data }: P) {
  if (!data) return null;
  const cents = toCents(data.margin);
  return (
    <span className="flex flex-col items-end leading-tight">
      <span className={`num ${cents < 0 ? "text-bad" : cents === 0 ? "text-ink-muted" : "text-ink"}`}>{formatMoney(data.margin, data.currency)}</span>
      {!isTotals(data) && toCents(data.cash_in) > 0 && <span className="num text-[11px] text-ink-muted">{data.margin_pct}%</span>}
    </span>
  );
}

export function PayrollCell({ data }: P) {
  if (!data || isTotals(data)) return null;
  return <PayrollBadge status={data.payroll_status} />;
}

export function ActionCell({ data, context }: P) {
  if (!data || isTotals(data) || !data.discrepancy_id) return null;
  const id = data.discrepancy_id;
  const s = data.discrepancy_status;
  const label = s === "PENDING_APPROVAL" ? "Review invoice" : s === "REMEDIATED" ? "View invoice" : s === "BLOCKED" ? "Why blocked" : "Details";
  const primary = s === "PENDING_APPROVAL";
  return (
    <button
      type="button"
      onClick={() => context.openRemediation(id)}
      className={`h-8 whitespace-nowrap rounded border px-3 text-[13px] font-medium transition-colors ${
        primary ? "border-ink bg-ink text-white hover:bg-brand-dark" : "border-transparent text-ink underline decoration-paper-line underline-offset-4 hover:decoration-ink"
      }`}
    >
      {label}
    </button>
  );
}

/** Full-width row under an expanded milestone: the clause, the AI summary and the money trail. */
export function DetailRow({ data, context }: P) {
  if (!data || isTotals(data)) return null;
  const r = data;
  return (
    <div className="fd-detail-row grid h-full gap-8 overflow-y-auto px-6 py-5 text-[13.5px] md:grid-cols-[1.4fr_1fr_1fr]">
      <div className="min-w-0">
        <p className="text-[13px] font-medium text-ink-soft">The clause it breaks</p>
        {r.violated_clause_quote ? (
          <p className="mt-1.5 whitespace-normal leading-relaxed text-ink">
            <span className="hl-soft">{r.violated_clause_quote}</span>
          </p>
        ) : (
          <p className="mt-1.5 text-ink-muted">{r.discrepancy_type === "COMPLIANT" ? "None. Paid within the terms." : "No payment has arrived yet."}</p>
        )}
        {r.ai_audit_summary && (
          <>
            <p className="mt-4 text-[13px] font-medium text-ink-soft">What the auditor wrote</p>
            <p className="mt-1 whitespace-normal leading-relaxed text-ink-soft">{r.ai_audit_summary}</p>
          </>
        )}
      </div>
      <dl className="grid grid-cols-[auto_1fr] content-start gap-x-4 gap-y-1.5">
        <dt className="text-ink-muted">Captured</dt>
        <dd className="num text-ink">{r.captured_at ? formatDateTime(r.captured_at) : "Not yet"}</dd>
        <dt className="text-ink-muted">Order</dt>
        <dd className="num truncate text-ink">{r.paypal_order_id ?? "None"}</dd>
        <dt className="text-ink-muted">Webhook</dt>
        <dd className="text-ink">{r.paypal_capture_id ? (r.webhook_verified ? "Verified signature" : "Pending") : "None"}</dd>
        <dt className="text-ink-muted">Payer</dt>
        <dd className="truncate text-ink">{r.client_paypal_email}</dd>
      </dl>
      <dl className="grid grid-cols-[auto_1fr] content-start gap-x-4 gap-y-1.5">
        <dt className="text-ink-muted">Invoice</dt>
        <dd className="num truncate text-ink">{r.remediation_invoice?.paypal_invoice_id ?? "Not sent"}</dd>
        <dt className="text-ink-muted">Sent by</dt>
        <dd className="text-ink">{r.remediation_sent_by === "leash-auto" ? "Leash (automatic)" : r.remediation_sent_by === "owner" ? "Owner" : "None"}</dd>
        <dt className="text-ink-muted">Cash in</dt>
        <dd className="num text-ink">{formatMoney(r.cash_in, r.currency)}</dd>
        <dt className="text-ink-muted">Team</dt>
        <dd className="num text-ink">
          {formatMoney(r.paid_to_team, r.currency)} paid
          {toCents(r.pending_to_team) > 0 && `, ${formatMoney(r.pending_to_team, r.currency)} pending`}
        </dd>
        {r.discrepancy_id && (
          <dd className="col-span-2 pt-1">
            <button type="button" className="text-[13px] font-medium text-ink underline underline-offset-4" onClick={() => context.openRemediation(r.discrepancy_id!)}>
              Open the correction invoice
            </button>
          </dd>
        )}
      </dl>
    </div>
  );
}

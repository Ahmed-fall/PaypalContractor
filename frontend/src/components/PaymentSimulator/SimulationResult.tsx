import { Link } from "react-router-dom";
import type { LedgerRow, SimulatorPaymentResponse } from "../../types/contracts";
import { ComplianceBadge, DecisionBadge } from "../common/StatusBadge";
import { InvoiceQr } from "../RemediationModal/InvoiceQr";
import { formatMoney, toCents } from "../../lib/money";

/** What one simulated payment did, step by step: capture, audit, Leash, invoice. */
export function SimulationResult({ res, row }: { res: SimulatorPaymentResponse; row: LedgerRow | null }) {
  const ev = res.evaluation;
  const c = row?.currency ?? "USD";
  const steps = [
    {
      title: "PayPal captured the payment",
      body: (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-ink-muted">Status</dt>
          <dd className="font-mono text-good">{res.capture_status}</dd>
          <dt className="text-ink-muted">Order</dt>
          <dd className="truncate font-mono text-ink">{res.paypal_order_id}</dd>
          <dt className="text-ink-muted">Capture</dt>
          <dd className="truncate font-mono text-ink" data-testid="sim-capture-id">
            {res.paypal_capture_id}
          </dd>
        </dl>
      ),
    },
    {
      title: "Code audited it against the contract",
      body: (
        <div className="space-y-3">
          <ComplianceBadge type={ev.discrepancy_type} />
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
            {[
              ["Expected", ev.expected_amount],
              ["Captured", ev.captured_amount],
              ["Shortfall", ev.shortfall_amount],
              ["Penalty", ev.penalty_amount],
            ].map(([k, v]) => (
              <div key={k}>
                <p className="text-xs text-ink-muted">{k}</p>
                <p className="font-mono tabular-nums text-ink">{formatMoney(v, k === "Captured" ? (ev.captured_currency ?? c) : c)}</p>
              </div>
            ))}
          </div>
          {toCents(ev.total_remediation_amount) > 0 && (
            <p className="text-sm">
              Owed: <span className="font-mono font-semibold text-bad">{formatMoney(ev.total_remediation_amount, c)}</span>
            </p>
          )}
          {ev.violated_clause_quote && <blockquote className="border-l-2 border-amber-400 pl-3 text-sm text-ink-soft">{ev.violated_clause_quote}</blockquote>}
          <p className="text-sm text-ink-soft">{ev.ai_audit_summary}</p>
        </div>
      ),
    },
  ];

  if (!ev.is_compliant) {
    steps.push({
      title: "Leash decided what may happen next",
      body: res.leash ? (
        <div className="space-y-2" data-testid="sim-leash">
          <div className="flex flex-wrap items-center gap-2">
            <DecisionBadge decision={res.leash.decision} />
            {res.leash.rule_ids.length > 0 && <span className="font-mono text-xs text-ink-muted">rule {res.leash.rule_ids.join(", ")}</span>}
          </div>
          <p className="text-sm text-ink-soft">{res.leash.reason}</p>
        </div>
      ) : (
        <p className="text-sm text-ink-soft">Leash is switched off, so the correction invoice waits for your approval.</p>
      ),
    });
    steps.push({
      title: res.remediation_invoice ? "PayPal invoice sent automatically" : res.leash?.decision === "DENY" ? "Invoice blocked" : "Invoice waiting for you",
      body: res.remediation_invoice ? (
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
          <InvoiceQr base64={res.remediation_invoice.qr_code_base64} payerUrl={res.remediation_invoice.payer_view_url} size={132} />
          <div className="text-sm">
            <p className="font-mono font-medium text-ink" data-testid="sim-invoice-id">
              {res.remediation_invoice.paypal_invoice_id}
            </p>
            <p className="mt-1 text-ink-muted">
              {formatMoney(res.remediation_invoice.total_amount, res.remediation_invoice.currency)} to {res.remediation_invoice.recipient_email}
            </p>
            <a href={res.remediation_invoice.payer_view_url} target="_blank" rel="noreferrer noopener" className="mt-2 inline-block font-medium text-brand hover:underline">
              Open the payer's view ↗
            </a>
          </div>
        </div>
      ) : res.discrepancy_id ? (
        <Link
          to={`/ledger?discrepancy=${encodeURIComponent(res.discrepancy_id)}`}
          className="inline-flex h-9 items-center rounded-md bg-ink px-3 text-sm font-medium text-white hover:bg-ink-soft"
        >
          {res.leash?.decision === "DENY" ? "See why in the ledger" : "Review & send in the ledger"}
        </Link>
      ) : (
        <span />
      ),
    });
  }

  return (
    <ol className="relative space-y-5 border-l border-paper-line pl-6" data-testid="sim-result">
      {steps.map((s, i) => (
        <li key={s.title} className="relative">
          <span className="absolute -left-[33px] flex h-5 w-5 items-center justify-center rounded-full bg-ink text-[10px] font-semibold text-white">{i + 1}</span>
          <p className="text-sm font-semibold text-ink">{s.title}</p>
          <div className="mt-2">{s.body}</div>
        </li>
      ))}
      <li className="relative flex flex-wrap gap-3 pt-1">
        <Link to="/ledger" className="text-sm font-medium text-brand hover:underline">
          See it in the ledger →
        </Link>
        {res.payroll_run_id && (
          <Link to={`/leash/payroll/${encodeURIComponent(res.payroll_run_id)}`} className="text-sm font-medium text-brand hover:underline">
            Team payroll prepared ({res.payroll_run_id}) →
          </Link>
        )}
      </li>
    </ol>
  );
}

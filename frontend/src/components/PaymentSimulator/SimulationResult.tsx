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
      title: "PayPal took the payment",
      body: (
        <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1 text-[14px]">
          <dt className="text-ink-muted">Status</dt>
          <dd className="text-good">{res.capture_status === "COMPLETED" ? "Completed" : res.capture_status}</dd>
          <dt className="text-ink-muted">Order</dt>
          <dd className="truncate num text-ink">{res.paypal_order_id}</dd>
          <dt className="text-ink-muted">Capture</dt>
          <dd className="truncate num text-ink" data-testid="sim-capture-id">
            {res.paypal_capture_id}
          </dd>
        </dl>
      ),
    },
    {
      title: "It was checked against the contract",
      body: (
        <div className="space-y-3">
          <ComplianceBadge type={ev.discrepancy_type} />
          <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-[14px] sm:grid-cols-4">
            {[
              ["Expected", ev.expected_amount],
              ["Received", ev.captured_amount],
              ["Short paid", ev.shortfall_amount],
              ["Penalty", ev.penalty_amount],
            ].map(([k, v]) => (
              <div key={k}>
                <p className="text-[13px] text-ink-muted">{k}</p>
                <p className="num text-[16px] text-ink">{formatMoney(v, k === "Received" ? (ev.captured_currency ?? c) : c)}</p>
              </div>
            ))}
          </div>
          {toCents(ev.total_remediation_amount) > 0 && (
            <p className="text-[14px] text-ink-soft">
              Owed to you <span className="num hl ml-1 text-[16px] font-semibold text-ink">{formatMoney(ev.total_remediation_amount, c)}</span>
            </p>
          )}
          {ev.violated_clause_quote && <p className="text-[14px] leading-relaxed text-ink-soft">Breaks: <span className="text-ink">{ev.violated_clause_quote}</span></p>}
          <p className="text-[14px] leading-relaxed text-ink-soft">{ev.ai_audit_summary}</p>
        </div>
      ),
    },
  ];

  if (!ev.is_compliant) {
    steps.push({
      title: "Leash decided what happens next",
      body: res.leash ? (
        <div className="space-y-2" data-testid="sim-leash">
          <div className="flex flex-wrap items-center gap-2">
            <DecisionBadge decision={res.leash.decision} />
            {res.leash.rule_ids.length > 0 && <span className="text-[13px] text-ink-muted">rule {res.leash.rule_ids.join(", ")}</span>}
          </div>
          <p className="text-[14px] text-ink-soft">{res.leash.reason}</p>
        </div>
      ) : (
        <p className="text-[14px] text-ink-soft">Leash is switched off, so the correction invoice waits for your approval.</p>
      ),
    });
    steps.push({
      title: res.remediation_invoice ? "The correction invoice went out" : res.leash?.decision === "DENY" ? "The invoice was blocked" : "The invoice is waiting for you",
      body: res.remediation_invoice ? (
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
          <InvoiceQr base64={res.remediation_invoice.qr_code_base64} payerUrl={res.remediation_invoice.payer_view_url} size={132} />
          <div className="text-[14px]">
            <p className="num text-[15px] font-medium text-ink" data-testid="sim-invoice-id">
              {res.remediation_invoice.paypal_invoice_id}
            </p>
            <p className="mt-1 text-ink-muted">
              {formatMoney(res.remediation_invoice.total_amount, res.remediation_invoice.currency)} to {res.remediation_invoice.recipient_email}
            </p>
            <a href={res.remediation_invoice.payer_view_url} target="_blank" rel="noreferrer noopener" className="mt-2 inline-block font-medium text-ink underline underline-offset-4">
              Open the invoice as the client sees it
            </a>
          </div>
        </div>
      ) : res.discrepancy_id ? (
        <Link
          to={`/ledger?discrepancy=${encodeURIComponent(res.discrepancy_id)}`}
          className="inline-flex h-9 items-center rounded border border-ink bg-ink px-3 text-[14px] font-medium text-white hover:bg-brand-dark"
        >
          {res.leash?.decision === "DENY" ? "See why in the ledger" : "Review the invoice"}
        </Link>
      ) : (
        <span />
      ),
    });
  }

  return (
    <ol className="relative space-y-7 border-l border-paper-line pl-7" data-testid="sim-result">
      {steps.map((s, i) => (
        <li key={s.title} className="relative">
          <span className="num absolute -left-[39px] flex h-6 w-6 items-center justify-center rounded-full border border-ink bg-white text-[12px] font-medium text-ink">{i + 1}</span>
          <p className="text-[15px] font-semibold text-ink">{s.title}</p>
          <div className="mt-2">{s.body}</div>
        </li>
      ))}
      <li className="relative flex flex-wrap gap-3 pt-1">
        <Link to="/ledger" className="text-[14px] font-medium text-ink underline underline-offset-4">
          See it in the ledger
        </Link>
        {res.payroll_run_id && (
          <Link to={`/leash/payroll/${encodeURIComponent(res.payroll_run_id)}`} className="text-[14px] font-medium text-ink underline underline-offset-4">
            Review the team payroll it prepared
          </Link>
        )}
      </li>
    </ol>
  );
}

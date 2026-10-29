import { useCallback, useEffect, useRef, useState } from "react";
import type { DecisionLogEntry, LedgerRow, RemediationInvoiceRecord, RemediationResponse } from "../../types/contracts";
import { api, isApiError } from "../../api/client";
import { Modal } from "../common/Modal";
import { Button, Spinner } from "../common/Button";
import { ComplianceBadge, LifecycleBadge } from "../common/StatusBadge";
import { InvoiceQr } from "./InvoiceQr";
import { formatMoney } from "../../lib/money";
import { formatDate, formatDateTime } from "../../lib/dates";

interface Props {
  row: LedgerRow | null;
  open: boolean;
  onClose: () => void;
  onSent: (res: RemediationResponse) => void;
  onStale: () => void; // ask the page to reload the ledger
}

function MathRow({ op, label, value, currency, total }: { op?: string; label: string; value: string | null; currency: string; total?: boolean }) {
  return (
    <div className={`grid grid-cols-[18px_1fr_auto] items-baseline gap-x-2 py-1.5 ${total ? "mt-1 border-t-[3px] border-double border-ink pt-2.5" : ""}`}>
      <span aria-hidden className="text-center text-ink-muted">{op ?? ""}</span>
      <span className={total ? "font-semibold text-ink" : "text-ink-soft"}>{label}</span>
      <span className={`num text-right ${total ? "text-[18px] font-semibold text-ink" : "text-ink"}`}>
        {total ? <span className="hl">{formatMoney(value, currency)}</span> : formatMoney(value, currency)}
      </span>
    </div>
  );
}

/**
 * Correction invoice review (plan WP-3.3, A.5). Shows the math, the clause and the AI
 * note. Approve & Send appears only while the discrepancy waits for the owner (Leash
 * said Ask); it locks on the first click and reuses one idempotency key, so a double
 * click or a retry can never send two invoices.
 */
export function RemediationModal({ row, open, onClose, onSent, onStale }: Props) {
  const [decision, setDecision] = useState<DecisionLogEntry | null>(null);
  const [decisionLoading, setDecisionLoading] = useState(false);
  const [inFlight, setInFlight] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<RemediationInvoiceRecord | null>(null);
  const lock = useRef(false);
  const discrepancyId = row?.discrepancy_id ?? null;

  useEffect(() => {
    setError(null);
    setSent(null);
    setInFlight(false);
    lock.current = false;
    setDecision(null);
    if (!open || !discrepancyId) return;
    let alive = true;
    setDecisionLoading(true);
    api
      .decisions({ subject_id: discrepancyId, subject_type: "discrepancy" })
      .then((res) => {
        if (!alive) return;
        // Newest first; prefer Leash's own decision over a later owner entry.
        setDecision(res.items.find((d) => d.subject_id === discrepancyId && d.actor === "leash") ?? res.items[0] ?? null);
      })
      .catch(() => alive && setDecision(null)) // Leash disabled or unavailable: the modal still works
      .finally(() => alive && setDecisionLoading(false));
    return () => {
      alive = false;
    };
  }, [open, discrepancyId]);

  const approve = useCallback(async () => {
    if (!row?.discrepancy_id || lock.current) return;
    lock.current = true; // synchronous guard: the second click of a double click stops here
    setInFlight(true);
    setError(null);
    try {
      const res = await api.remediate(row.discrepancy_id, {
        confirm_send: true,
        custom_note_override: null,
        idempotency_key: `rem_${row.discrepancy_id}`,
      });
      setSent(res.remediation_invoice);
      onSent(res);
    } catch (e) {
      if (isApiError(e) && e.code === "ALREADY_REMEDIATED") {
        setError("This invoice was already sent. Refreshing the ledger.");
        onStale();
      } else if (isApiError(e) && e.code === "LEASH_DENIED") {
        setError(`Leash denied this invoice: ${e.message}`);
        onStale();
      } else if (isApiError(e) && e.code === "DEMO_KEY_REQUIRED") {
        setError("Unlock with the owner key, then approve again.");
        lock.current = false;
      } else {
        const msg = e instanceof Error ? e.message : "Sending failed.";
        const rid = isApiError(e) && e.requestId ? ` (request ${e.requestId})` : "";
        setError(`${msg}${rid} You can retry safely; PayPal will not receive a second invoice.`);
        lock.current = false; // same idempotency key on retry
      }
    } finally {
      setInFlight(false);
    }
  }, [row, onSent, onStale]);

  if (!row) {
    return (
      <Modal open={open} onClose={onClose} title="Discrepancy not found" width="sm">
        <p className="text-sm text-ink-muted">This discrepancy is not in the current ledger. It may have been reset.</p>
      </Modal>
    );
  }

  const invoice = sent ?? row.remediation_invoice;
  const status = sent ? "REMEDIATED" : row.discrepancy_status;
  const sentBy = sent ? "owner" : row.remediation_sent_by;
  const canApprove = status === "PENDING_APPROVAL" && !invoice;
  const blocked = status === "BLOCKED";
  const c = row.currency;

  const footer = canApprove ? (
    <>
      <Button variant="ghost" onClick={onClose} disabled={inFlight}>
        Not now
      </Button>
      <Button onClick={approve} disabled={inFlight || lock.current} data-testid="approve-send" aria-busy={inFlight}>
        {inFlight ? (
          <>
            <Spinner /> Sending…
          </>
        ) : (
          `Approve and send ${formatMoney(row.total_remediation_amount, c)}`
        )}
      </Button>
    </>
  ) : (
    <Button variant="secondary" onClick={onClose}>
      Close
    </Button>
  );

  return (
    <Modal
      open={open}
      onClose={onClose}
      width="lg"
      title={`${row.client_name}, ${row.milestone_code}`}
      subtitle={
        <span className="flex flex-wrap items-center gap-2 pt-1">
          <ComplianceBadge type={row.discrepancy_type} />
          <LifecycleBadge status={status} sentBy={sentBy} decision={row.leash_decision} />
          <span className="num text-[12.5px] text-ink-muted">{row.discrepancy_id}</span>
        </span>
      }
      footer={footer}
    >
      {sentBy === "leash-auto" && invoice && (
        <div className="mb-6 border-l-[3px] border-good py-1 pl-4 text-[14px]" data-testid="leash-auto-banner">
          <p className="font-semibold text-good">Sent automatically by Leash (rule {decision?.rule_ids[0] ?? "G-01"})</p>
          <p className="mt-0.5 text-ink-soft">{decision?.reason ?? "Within your auto-send limit and the breached clause is quoted."}</p>
        </div>
      )}
      {canApprove && (
        <div className="mb-6 border-l-[3px] border-bad py-1 pl-4 text-[14px]">
          <p className="flex flex-wrap items-center gap-2 font-semibold text-ink">
            Leash wants your approval before sending
            {decision?.rule_ids.length ? <span className="text-[13px] font-normal text-ink-muted">rule {decision.rule_ids.join(", ")}</span> : null}
          </p>
          <p className="mt-0.5 text-ink-soft">
            {decision?.reason ?? (decisionLoading ? "Loading Leash's reason…" : "Review the amount and clause, then approve to send the PayPal invoice.")}
          </p>
        </div>
      )}
      {blocked && (
        <div className="mb-6 border-l-[3px] border-bad py-1 pl-4 text-[14px]">
          <p className="font-semibold text-bad">Blocked by Leash{decision?.rule_ids.length ? ` (rule ${decision.rule_ids.join(", ")})` : ""}</p>
          <p className="mt-0.5 text-ink-soft">{decision?.reason ?? "Leash will not send this invoice. Edit the contract terms to re-evaluate it."}</p>
        </div>
      )}

      <div className="grid gap-8 md:grid-cols-[1fr_1.1fr]">
        <section aria-label="How the amount was computed">
          <h3 className="text-[14px] font-semibold text-ink">How {formatMoney(row.total_remediation_amount, c)} is owed</h3>
          <div className="mt-3 text-[14px]">
            <MathRow label={`Contract amount, due ${formatDate(row.due_date)}`} value={row.expected_amount} currency={c} />
            <MathRow op="−" label={`Received${row.captured_at ? ` on ${formatDate(row.captured_at)}` : ""}`} value={row.captured_amount} currency={row.captured_currency ?? c} />
            <MathRow op="=" label="Short paid" value={row.shortfall_amount} currency={c} />
            <MathRow op="+" label="Contract penalty" value={row.penalty_amount} currency={c} />
            <MathRow label="Correction invoice" value={row.total_remediation_amount} currency={c} total />
          </div>
          <p className="mt-3 text-[12.5px] leading-relaxed text-ink-muted">Calculated in code from the terms you activated. The AI never sets an amount.</p>
        </section>

        <section className="space-y-5">
          <div>
            <h3 className="text-[14px] font-semibold text-ink">The clause it breaks</h3>
            <p className="mt-2 text-[14px] leading-relaxed text-ink">
              {row.violated_clause_quote ? <span className="hl-soft">{row.violated_clause_quote}</span> : "No clause recorded."}
            </p>
          </div>
          <div>
            <h3 className="text-[14px] font-semibold text-ink">Note printed on the invoice</h3>
            <p className="mt-2 text-[14px] leading-relaxed text-ink-soft">{row.ai_audit_summary ?? "No note."}</p>
          </div>
        </section>
      </div>

      {error && (
        <p role="alert" className="mt-5 rounded bg-bad-soft px-3 py-2 text-sm text-bad">
          {error}
        </p>
      )}

      {invoice && (
        <section className="mt-7 border-t border-paper-line pt-6" data-testid="invoice-panel">
          <div className="flex flex-col gap-5 sm:flex-row sm:items-center">
            <InvoiceQr base64={invoice.qr_code_base64} payerUrl={invoice.payer_view_url} />
            <dl className="grid min-w-0 flex-1 grid-cols-[auto_1fr] gap-x-5 gap-y-2 text-[14px]">
              <dt className="text-ink-muted">PayPal invoice</dt>
              <dd className="num truncate font-medium text-ink" data-testid="invoice-id">
                {invoice.paypal_invoice_id}
              </dd>
              <dt className="text-ink-muted">Number</dt>
              <dd className="num text-ink">{invoice.paypal_invoice_number}</dd>
              <dt className="text-ink-muted">Amount</dt>
              <dd className="num text-ink">{formatMoney(invoice.total_amount, invoice.currency)}</dd>
              <dt className="text-ink-muted">Sent to</dt>
              <dd className="truncate text-ink">{invoice.recipient_email}</dd>
              <dt className="text-ink-muted">Status</dt>
              <dd className="text-ink">
                {invoice.status === "SENT" ? "Sent" : invoice.status}
                {invoice.sent_at ? `, ${formatDateTime(invoice.sent_at)}` : ""}
              </dd>
              <dd className="col-span-2 pt-1">
                <a href={invoice.payer_view_url} target="_blank" rel="noreferrer noopener" className="text-[14px] font-medium text-ink underline underline-offset-4">
                  Open the invoice as the client sees it
                </a>
              </dd>
            </dl>
          </div>
        </section>
      )}
    </Modal>
  );
}

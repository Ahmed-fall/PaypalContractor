import { useCallback, useEffect, useRef, useState } from "react";
import type { DecisionLogEntry, LedgerRow, RemediationInvoiceRecord, RemediationResponse } from "../../types/contracts";
import { api, isApiError } from "../../api/client";
import { Modal } from "../common/Modal";
import { Button, Spinner } from "../common/Button";
import { ComplianceBadge, DecisionBadge, LifecycleBadge } from "../common/StatusBadge";
import { InvoiceQr } from "./InvoiceQr";
import { formatMoney, toCents } from "../../lib/money";
import { formatDate, formatDateTime } from "../../lib/dates";

interface Props {
  row: LedgerRow | null;
  open: boolean;
  onClose: () => void;
  onSent: (res: RemediationResponse) => void;
  onStale: () => void; // ask the page to reload the ledger
}

function MathRow({ label, value, currency, strong, tone }: { label: string; value: string | null; currency: string; strong?: boolean; tone?: "bad" }) {
  return (
    <div className={`flex items-baseline justify-between gap-4 py-1.5 ${strong ? "border-t border-paper-line pt-2.5 text-base font-semibold" : "text-sm"}`}>
      <span className={strong ? "text-ink" : "text-ink-muted"}>{label}</span>
      <span className={`font-mono tabular-nums ${tone === "bad" && toCents(value) > 0 ? "text-bad" : "text-ink"}`}>{formatMoney(value, currency)}</span>
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
        setError("Unlock with the owner key, then press Approve & Send again.");
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
          `Approve & Send ${formatMoney(row.total_remediation_amount, c)}`
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
      title={`${row.milestone_code} · ${row.client_name}`}
      subtitle={
        <span className="flex flex-wrap items-center gap-2 pt-1">
          <ComplianceBadge type={row.discrepancy_type} />
          <LifecycleBadge status={status} sentBy={sentBy} decision={row.leash_decision} />
          <span className="font-mono text-xs">{row.discrepancy_id}</span>
        </span>
      }
      footer={footer}
    >
      {sentBy === "leash-auto" && invoice && (
        <div className="mb-5 rounded-lg border border-good/25 bg-good-soft px-4 py-3 text-sm" data-testid="leash-auto-banner">
          <p className="font-semibold text-good">Sent automatically by Leash (rule {decision?.rule_ids[0] ?? "G-01"})</p>
          <p className="mt-0.5 text-ink-soft">{decision?.reason ?? "Within your auto-send limit and the breached clause is quoted."}</p>
        </div>
      )}
      {canApprove && (
        <div className="mb-5 rounded-lg border border-brand/25 bg-brand-soft px-4 py-3 text-sm">
          <p className="flex flex-wrap items-center gap-2 font-semibold text-brand-dark">
            Leash is asking for your approval
            {decision && <DecisionBadge decision={decision.decision} />}
            {decision?.rule_ids.length ? <span className="font-mono text-xs font-normal">rule {decision.rule_ids.join(", ")}</span> : null}
          </p>
          <p className="mt-0.5 text-ink-soft">
            {decision?.reason ?? (decisionLoading ? "Loading Leash's reason…" : "Review the amount and clause, then approve to send the PayPal invoice.")}
          </p>
        </div>
      )}
      {blocked && (
        <div className="mb-5 rounded-lg border border-bad/25 bg-bad-soft px-4 py-3 text-sm">
          <p className="font-semibold text-bad">Blocked by Leash{decision?.rule_ids.length ? ` (rule ${decision.rule_ids.join(", ")})` : ""}</p>
          <p className="mt-0.5 text-ink-soft">{decision?.reason ?? "Leash will not send this invoice. Edit the contract terms to re-evaluate it."}</p>
        </div>
      )}

      <div className="grid gap-6 md:grid-cols-[1fr_1.15fr]">
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-muted">How we got to {formatMoney(row.total_remediation_amount, c)}</h3>
          <div className="mt-2 rounded-lg border border-paper-line px-4 py-2">
            <MathRow label={`Expected (due ${formatDate(row.due_date)})`} value={row.expected_amount} currency={c} />
            <MathRow label={`Captured${row.captured_at ? ` (${formatDate(row.captured_at)})` : ""}`} value={row.captured_amount} currency={row.captured_currency ?? c} />
            <MathRow label="Shortfall" value={row.shortfall_amount} currency={c} tone="bad" />
            <MathRow label="Contract penalty" value={row.penalty_amount} currency={c} tone="bad" />
            <MathRow label="Correction invoice" value={row.total_remediation_amount} currency={c} strong />
          </div>
          <p className="mt-2 text-[11px] text-ink-muted">Computed in code from the activated contract terms. The AI never sets an amount.</p>
        </section>

        <section className="space-y-4">
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-muted">Clause</h3>
            <blockquote className="mt-2 border-l-2 border-amber-400 pl-3 text-sm leading-relaxed text-ink">
              {row.violated_clause_quote ?? "No clause recorded."}
            </blockquote>
          </div>
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-muted">AI note on the invoice</h3>
            <p className="mt-2 text-sm leading-relaxed text-ink-soft">{row.ai_audit_summary ?? "No summary."}</p>
          </div>
        </section>
      </div>

      {error && (
        <p role="alert" className="mt-5 rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">
          {error}
        </p>
      )}

      {invoice && (
        <section className="mt-6 rounded-xl border border-paper-line bg-paper/50 p-4" data-testid="invoice-panel">
          <div className="flex flex-col gap-5 sm:flex-row sm:items-center">
            <InvoiceQr base64={invoice.qr_code_base64} payerUrl={invoice.payer_view_url} />
            <dl className="grid min-w-0 flex-1 grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
              <dt className="text-ink-muted">PayPal invoice</dt>
              <dd className="truncate font-mono font-medium text-ink" data-testid="invoice-id">
                {invoice.paypal_invoice_id}
              </dd>
              <dt className="text-ink-muted">Number</dt>
              <dd className="font-mono text-ink">{invoice.paypal_invoice_number}</dd>
              <dt className="text-ink-muted">Amount</dt>
              <dd className="font-mono text-ink">{formatMoney(invoice.total_amount, invoice.currency)}</dd>
              <dt className="text-ink-muted">Sent to</dt>
              <dd className="truncate text-ink">{invoice.recipient_email}</dd>
              <dt className="text-ink-muted">Status</dt>
              <dd className="text-ink">
                {invoice.status}
                {invoice.sent_at ? ` · ${formatDateTime(invoice.sent_at)}` : ""}
              </dd>
              <dd className="col-span-2 pt-1">
                <a href={invoice.payer_view_url} target="_blank" rel="noreferrer noopener" className="text-sm font-medium text-brand hover:underline">
                  Open the payer's view on PayPal ↗
                </a>
              </dd>
            </dl>
          </div>
        </section>
      )}
    </Modal>
  );
}

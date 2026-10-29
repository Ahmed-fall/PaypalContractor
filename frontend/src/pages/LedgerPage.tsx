import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { ReconciliationGrid, computeTotals, joinMargin, type TotalsRow } from "../components/ReconciliationGrid";
import { RemediationModal } from "../components/RemediationModal";
import { PageHeader } from "../components/common/Card";
import { Button } from "../components/common/Button";
import { on } from "../lib/events";
import { centsToString, formatMoney, toCents } from "../lib/money";
import type { LedgerResponse, MarginResponse, RemediationResponse } from "../types/contracts";

function Figure({ label, value, sub, strong }: { label: string; value: string; sub?: ReactNode; strong?: boolean }) {
  return (
    <div className="min-w-0">
      <p className="text-[13px] text-ink-muted">{label}</p>
      <p className={`num mt-1 whitespace-nowrap leading-none tracking-[-0.02em] ${strong ? "text-[34px] font-semibold text-ink" : "text-[26px] font-medium text-ink"}`}>
        {strong ? <span className="hl">{value}</span> : value}
      </p>
      {sub && <p className="mt-2 text-[13px] text-ink-muted">{sub}</p>}
    </div>
  );
}

function Op({ children }: { children: string }) {
  return (
    <span aria-hidden className="hidden pt-[21px] text-[26px] font-light leading-[30px] text-ink-muted sm:block">
      {children}
    </span>
  );
}

/**
 * The page's one bold element: the reconciliation written as the equation it is.
 * Every figure comes from the same cents-based totals as the grid's pinned row.
 */
function Statement({ ledger, totals, margin }: { ledger: LedgerResponse | null; totals: TotalsRow | null; margin: MarginResponse | null }) {
  const c = totals?.currency ?? "USD";
  const owed = centsToString(toCents(totals?.shortfall_amount) + toCents(totals?.penalty_amount));
  const invoiced = ledger?.summary.total_remediation_invoiced ?? "0.00";
  const pending = ledger?.rows.filter((r) => r.discrepancy_status === "PENDING_APPROVAL").length ?? 0;
  return (
    <section aria-label="Reconciliation summary" className="mb-8 border-y border-ink/80 py-6">
      <div className="flex flex-wrap items-start gap-x-10 gap-y-6">
        <Figure label="Received on PayPal" value={formatMoney(totals?.captured_amount, c)} sub={`of ${formatMoney(totals?.expected_amount, c)} the contracts expect`} />
        <span aria-hidden className="hidden w-px self-stretch bg-paper-line lg:block" />
        <div className="flex flex-wrap items-start gap-x-6 gap-y-6">
          <Figure label="Short paid" value={formatMoney(totals?.shortfall_amount, c)} />
          <Op>+</Op>
          <Figure label="Contract penalties" value={formatMoney(totals?.penalty_amount, c)} />
          <Op>=</Op>
          <Figure
            label="Owed to you"
            value={formatMoney(owed, c)}
            strong
            sub={
              pending ? (
                <span className="font-medium text-bad">{pending === 1 ? "1 invoice waits for your approval" : `${pending} invoices wait for your approval`}</span>
              ) : (
                `${formatMoney(invoiced, c)} already invoiced`
              )
            }
          />
        </div>
        <dl className="ml-auto grid grid-cols-[auto_auto] gap-x-6 gap-y-1.5 self-center text-[14px]">
          <dt className="text-ink-muted">Invoiced</dt>
          <dd className="num text-right text-ink">{formatMoney(invoiced, c)}</dd>
          <dt className="text-ink-muted">Paid to team</dt>
          <dd className="num text-right text-ink">{margin ? formatMoney(totals?.paid_to_team, c) : "Not available"}</dd>
          <dt className="border-t border-paper-line pt-1.5 font-medium text-ink">Margin kept</dt>
          <dd className="num border-t border-paper-line pt-1.5 text-right font-medium text-good">{formatMoney(totals?.margin, c)}</dd>
        </dl>
      </div>
    </section>
  );
}

/** /ledger: the reconciliation grid, margin columns and the remediation modal at ?discrepancy=. */
export default function LedgerPage() {
  const [params, setParams] = useSearchParams();
  const [ledger, setLedger] = useState<LedgerResponse | null>(null);
  const [margin, setMargin] = useState<MarginResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    // The margin call is optional: with Leash off or failing, the grid coalesces to "0.00".
    const [l, m] = await Promise.allSettled([api.ledger(), api.margin()]);
    if (l.status === "fulfilled") {
      setLedger(l.value);
      setFailed(false);
    } else {
      setFailed(true);
    }
    setMargin(m.status === "fulfilled" ? m.value : null);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => on("ledger-changed", () => void load()), [load]);

  const discrepancyId = params.get("discrepancy");
  const row = useMemo(() => ledger?.rows.find((r) => r.discrepancy_id === discrepancyId) ?? null, [ledger, discrepancyId]);

  const openRemediation = useCallback(
    (id: string) => {
      const next = new URLSearchParams(params);
      next.set("discrepancy", id);
      setParams(next);
    },
    [params, setParams],
  );

  const close = useCallback(() => {
    const next = new URLSearchParams(params);
    next.delete("discrepancy");
    setParams(next, { replace: true });
  }, [params, setParams]);

  const onSent = useCallback((res: RemediationResponse) => {
    // Optimistic: the row turns REMEDIATED at once; a reload confirms from the server.
    setLedger((cur) =>
      cur
        ? {
            ...cur,
            rows: cur.rows.map((r) =>
              r.discrepancy_id === res.discrepancy_id
                ? { ...r, discrepancy_status: res.discrepancy_status, remediation_invoice: res.remediation_invoice, remediation_sent_by: "owner" }
                : r,
            ),
          }
        : cur,
    );
    void load();
  }, [load]);

  const totals = useMemo(() => (ledger ? computeTotals(joinMargin(ledger.rows, margin)) : null), [ledger, margin]);

  return (
    <div>
      <PageHeader
        title="Ledger"
        description="Each milestone, what its contract says you should have been paid, what actually arrived through PayPal, and what went out to your team."
        actions={
          <>
            <Button variant="secondary" onClick={() => void load()} disabled={loading}>
              Refresh
            </Button>
            <Link to="/simulator" className="inline-flex h-10 items-center rounded border border-ink bg-ink px-4 text-[14px] font-medium text-white hover:bg-brand-dark">
              Simulate a payment
            </Link>
          </>
        }
      />

      {failed && !ledger ? (
        <div className="rounded-lg border border-bad/30 bg-white px-6 py-10">
          <p className="font-medium text-bad">The ledger didn't load.</p>
          <p className="mt-1 text-[14px] text-ink-soft">The API isn't answering. Start the backend, then try again.</p>
          <Button className="mt-4" variant="secondary" onClick={() => void load()}>
            Try again
          </Button>
        </div>
      ) : (
        <>
          <Statement ledger={ledger} totals={totals} margin={margin} />
          <ReconciliationGrid rows={ledger?.rows ?? []} margin={margin} loading={loading && !ledger} onOpenRemediation={openRemediation} />
        </>
      )}

      <RemediationModal row={row} open={Boolean(discrepancyId) && Boolean(ledger)} onClose={close} onSent={onSent} onStale={() => void load()} />
    </div>
  );
}

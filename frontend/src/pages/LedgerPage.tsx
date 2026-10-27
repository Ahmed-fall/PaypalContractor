import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { ReconciliationGrid, computeTotals, joinMargin } from "../components/ReconciliationGrid";
import { RemediationModal } from "../components/RemediationModal";
import { PageHeader } from "../components/common/Card";
import { Button } from "../components/common/Button";
import { on } from "../lib/events";
import { formatMoney } from "../lib/money";
import type { LedgerResponse, MarginResponse, RemediationResponse } from "../types/contracts";

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "bad" | "good" }) {
  return (
    <div className="rounded-xl border border-paper-line bg-white px-4 py-3 shadow-card">
      <p className="text-[11px] font-semibold uppercase tracking-wider text-ink-muted">{label}</p>
      <p className={`mt-1 font-mono text-xl font-semibold tabular-nums ${tone === "bad" ? "text-bad" : tone === "good" ? "text-good" : "text-ink"}`}>{value}</p>
      {sub && <p className="mt-0.5 text-xs text-ink-muted">{sub}</p>}
    </div>
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
  const c = totals?.currency ?? "USD";
  const pending = ledger?.rows.filter((r) => r.discrepancy_status === "PENDING_APPROVAL").length ?? 0;

  return (
    <div>
      <PageHeader
        eyebrow="Detective"
        title="Reconciliation ledger"
        description="Every milestone, what the contract says you are owed, what PayPal actually captured, and what went out to the team."
        actions={
          <>
            <Button variant="secondary" onClick={() => void load()} disabled={loading}>
              Refresh
            </Button>
            <Link to="/simulator" className="inline-flex h-10 items-center rounded-md bg-ink px-4 text-sm font-medium text-white hover:bg-ink-soft">
              Simulate a payment
            </Link>
          </>
        }
      />

      {failed && !ledger ? (
        <div className="rounded-xl border border-bad/30 bg-bad-soft px-6 py-10 text-center">
          <p className="font-medium text-bad">The ledger could not be loaded.</p>
          <p className="mt-1 text-sm text-ink-soft">Check that the API is running, then try again.</p>
          <Button className="mt-4" variant="secondary" onClick={() => void load()}>
            Try again
          </Button>
        </div>
      ) : (
        <>
          <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <Stat label="Expected" value={formatMoney(totals?.expected_amount, c)} sub={`${totals?.rows ?? 0} milestones`} />
            <Stat label="Received" value={formatMoney(totals?.captured_amount, c)} sub="Same-currency captures" />
            <Stat label="Owed to you" value={formatMoney(totals?.total_remediation_amount, c)} tone="bad" sub={pending ? `${pending} awaiting your approval` : "Shortfall + penalties"} />
            <Stat label="Invoiced" value={formatMoney(ledger?.summary.total_remediation_invoiced, c)} sub="Correction invoices sent" />
            <Stat label="Paid to team" value={formatMoney(totals?.paid_to_team, c)} sub={margin ? "Through PayPal Payouts" : "Leash data unavailable"} />
            <Stat label="Margin" value={formatMoney(totals?.margin, c)} tone="good" sub="Cash in minus team pay" />
          </div>
          <ReconciliationGrid rows={ledger?.rows ?? []} margin={margin} loading={loading && !ledger} onOpenRemediation={openRemediation} />
        </>
      )}

      <RemediationModal row={row} open={Boolean(discrepancyId) && Boolean(ledger)} onClose={close} onSent={onSent} onStale={() => void load()} />
    </div>
  );
}

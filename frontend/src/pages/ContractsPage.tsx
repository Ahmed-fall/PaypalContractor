import { Link } from "react-router-dom";
import { PageHeader } from "../components/common/Card";
import { Pill } from "../components/common/StatusBadge";
import { listContracts } from "../lib/contractStore";
import { formatMoney, sumMoney } from "../lib/money";
import { formatDateTime } from "../lib/dates";

/** Hub for the Contracts nav item: start a new upload or reopen one from this tab. */
export default function ContractsPage() {
  const contracts = listContracts();
  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader
        eyebrow="Detective"
        title="Contracts"
        description="Upload a signed contract to turn its payment clauses into rules the ledger audits."
        actions={
          <Link to="/contracts/new" className="inline-flex h-10 items-center rounded-md bg-ink px-4 text-sm font-medium text-white hover:bg-ink-soft">
            Upload a contract
          </Link>
        }
      />
      {contracts.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white px-6 py-14 text-center">
          <p className="text-sm font-medium text-ink">No contracts reviewed in this tab yet</p>
          <p className="mt-1 text-sm text-ink-muted">Active contracts from the backend appear in the ledger.</p>
          <Link to="/contracts/new" className="mt-5 inline-block text-sm font-medium text-brand hover:underline">
            Upload your first contract
          </Link>
        </div>
      ) : (
        <ul className="divide-y divide-paper-line overflow-hidden rounded-xl border border-paper-line bg-white shadow-card">
          {contracts.map((c) => (
            <li key={c.contract_id}>
              <Link to={`/contracts/${c.contract_id}`} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4 hover:bg-paper/60">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-ink">{c.extraction.contract_title}</p>
                  <p className="mt-0.5 text-xs text-ink-muted">
                    {c.extraction.client_name} · {c.extraction.milestones.length} milestone{c.extraction.milestones.length === 1 ? "" : "s"} ·{" "}
                    {formatMoney(sumMoney(c.extraction.milestones.map((m) => m.expected_amount)), c.extraction.currency)} · updated {formatDateTime(c.updated_at)}
                  </p>
                </div>
                {c.status === "ACTIVE" ? <Pill tone="good">Active</Pill> : <Pill tone="neutral">Draft</Pill>}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

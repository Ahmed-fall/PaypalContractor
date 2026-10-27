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
        title="Contracts"
        description="Contracts you have reviewed in this tab. Active contracts from the server show up in the ledger."
        actions={
          <Link to="/contracts/new" className="inline-flex h-10 items-center rounded border border-ink bg-ink px-4 text-[14px] font-medium text-white hover:bg-brand-dark">
            Add a contract
          </Link>
        }
      />
      {contracts.length === 0 ? (
        <div className="rounded-lg border border-dashed border-paper-line bg-white px-6 py-14 text-center">
          <p className="text-[15px] font-medium text-ink">No contracts reviewed yet</p>
          <p className="mt-1 text-[14px] text-ink-muted">Upload a signed contract to turn its payment clauses into rules.</p>
          <Link to="/contracts/new" className="mt-5 inline-block text-[14px] font-medium text-ink underline underline-offset-4">
            Add a contract
          </Link>
        </div>
      ) : (
        <ul className="divide-y divide-paper-line overflow-hidden rounded-lg border border-paper-line bg-white">
          {contracts.map((c) => (
            <li key={c.contract_id}>
              <Link to={`/contracts/${c.contract_id}`} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4 hover:bg-paper/60">
                <div className="min-w-0">
                  <p className="truncate text-[15px] font-medium text-ink">{c.extraction.contract_title}</p>
                  <p className="mt-0.5 text-[13px] text-ink-muted">
                    {c.extraction.client_name}, {c.extraction.milestones.length} milestone{c.extraction.milestones.length === 1 ? "" : "s"} worth{" "}
                    {formatMoney(sumMoney(c.extraction.milestones.map((m) => m.expected_amount)), c.extraction.currency)}. Updated {formatDateTime(c.updated_at)}.
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

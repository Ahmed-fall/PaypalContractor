import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { PaymentSimulator } from "../components/PaymentSimulator";
import { PageHeader } from "../components/common/Card";
import type { LedgerRow } from "../types/contracts";

/** /simulator: the client pays, the Detective audits, Leash decides. */
export default function SimulatorPage() {
  const [params] = useSearchParams();
  const [rows, setRows] = useState<LedgerRow[] | null>(null);

  const load = useCallback(() => {
    api
      .ledger()
      .then((l) => setRows(l.rows))
      .catch(() => setRows([]));
  }, []);

  useEffect(load, [load]);

  return (
    <div>
      <PageHeader
        title="Simulate a client payment"
        description="Pay a milestone as the client would, through the PayPal sandbox. The payment is audited against its contract and Leash decides what happens next."
      />
      {rows === null ? (
        <div className="h-64 animate-pulse rounded-lg bg-white" />
      ) : (
        <PaymentSimulator rows={rows} initialMilestoneId={params.get("milestone")} />
      )}
    </div>
  );
}

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
        eyebrow="Detective"
        title="Client payment simulator"
        description="Play the client: pay a milestone through the PayPal sandbox and watch the audit, the Leash decision and the correction invoice happen."
      />
      {rows === null ? (
        <div className="h-64 animate-pulse rounded-xl bg-white" />
      ) : (
        <PaymentSimulator rows={rows} initialMilestoneId={params.get("milestone")} />
      )}
    </div>
  );
}

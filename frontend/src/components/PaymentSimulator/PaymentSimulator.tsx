import { useEffect, useMemo, useRef, useState } from "react";
import type { LedgerRow, SimulatorPaymentResponse } from "../../types/contracts";
import { api } from "../../api/client";
import { emit } from "../../lib/events";
import { randomSuffix } from "../../lib/ids";
import { formatMoney, isMoney } from "../../lib/money";
import { formatDate } from "../../lib/dates";
import { Button, Spinner } from "../common/Button";
import { ComplianceBadge } from "../common/StatusBadge";
import { SCENARIOS, buildRequest, type CustomInputs, type ScenarioId } from "./scenarios";
import { SimulationResult } from "./SimulationResult";

interface Props {
  rows: LedgerRow[];
  initialMilestoneId?: string | null;
}

const inputCls = "w-full rounded border border-paper-line bg-white px-3 py-2 text-[14px] outline-none focus:border-ink";

/** Client Payment Simulator (plan WP-3.3, REQ-FE-03): run a real sandbox order + capture. */
export function PaymentSimulator({ rows, initialMilestoneId }: Props) {
  const [milestoneId, setMilestoneId] = useState<string>(initialMilestoneId ?? "");
  const [scenario, setScenario] = useState<ScenarioId>("B");
  const [custom, setCustom] = useState<CustomInputs>({ amount: "", date: "", currency: "" });
  const [payer, setPayer] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ res: SimulatorPaymentResponse; row: LedgerRow } | null>(null);
  const lock = useRef(false);

  useEffect(() => {
    if (milestoneId || rows.length === 0) return;
    const preferred = rows.find((r) => r.milestone_id === initialMilestoneId) ?? rows.find((r) => !r.captured_amount) ?? rows[0];
    setMilestoneId(preferred.milestone_id);
  }, [rows, milestoneId, initialMilestoneId]);

  const row = useMemo(() => rows.find((r) => r.milestone_id === milestoneId) ?? null, [rows, milestoneId]);

  useEffect(() => {
    if (!row) return;
    setPayer(row.client_paypal_email);
    setCustom({ amount: row.expected_amount, date: row.due_date, currency: row.currency });
  }, [row]);

  const customError =
    scenario === "C" && row
      ? !isMoney(custom.amount) || Number(custom.amount) <= 0
        ? "Enter an amount like 850.00."
        : !/^\d{4}-\d{2}-\d{2}$/.test(custom.date)
          ? "Pick a payment date."
          : !/^[A-Za-z]{3}$/.test(custom.currency)
            ? "Use a 3-letter currency code."
            : null
      : null;

  const run = async () => {
    if (!row || lock.current || customError) return;
    lock.current = true;
    setBusy(true);
    try {
      const req = buildRequest(row, scenario, custom, payer, `sim_${row.milestone_id}_${randomSuffix()}`);
      const res = await api.simulatePay(req);
      setResult({ res, row });
      emit("ledger-changed", undefined);
    } catch {
      /* toast or unlock dialog already shown by the client */
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };

  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-paper-line bg-white px-6 py-12 text-center text-[14px] text-ink-muted">
        No milestones to pay yet. Activate a contract first.
      </div>
    );
  }

  const alreadyPaid = Boolean(row?.captured_amount);

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
      <div className="space-y-6 rounded-lg border border-paper-line bg-white p-5 sm:p-6">
        <label className="block">
          <span className="text-[13px] font-medium text-ink-soft">Milestone</span>
          <select className={`${inputCls} mt-1`} value={milestoneId} onChange={(e) => setMilestoneId(e.target.value)} data-testid="sim-milestone">
            {rows.map((r) => (
              <option key={r.milestone_id} value={r.milestone_id}>
                {r.client_name}, {r.milestone_code}, {formatMoney(r.expected_amount, r.currency)}
              </option>
            ))}
          </select>
        </label>

        {row && (
          <div className="-mt-3 flex flex-wrap items-center gap-3 text-[13px] text-ink-soft">
            <span>Due {formatDate(row.due_date)}</span>
            <ComplianceBadge type={row.discrepancy_type} />
          </div>
        )}
        {alreadyPaid && <p className="text-[13px] text-warn">This milestone already has a payment. Sending another replaces it in the demo ledger.</p>}

        <fieldset>
          <legend className="text-[13px] font-medium text-ink-soft">What the client pays</legend>
          <div className="mt-2 divide-y divide-paper-rule border-y border-paper-rule">
            {SCENARIOS.map((s) => (
              <label
                key={s.id}
                className={`flex cursor-pointer items-start gap-3 py-3 transition-colors ${scenario === s.id ? "text-ink" : "text-ink-soft hover:text-ink"}`}
              >
                <input type="radio" name="scenario" value={s.id} checked={scenario === s.id} onChange={() => setScenario(s.id)} className="mt-1 accent-[#16302F]" />
                <span>
                  <span className="text-[14px] font-medium">{s.title}</span>
                  <span className="num block text-[13px] text-ink-muted">{s.describe(row)}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        {scenario === "C" && (
          <div className="grid grid-cols-[1fr_1fr_80px] gap-3">
            <label className="block">
              <span className="text-[13px] font-medium text-ink-soft">Amount</span>
              <input inputMode="decimal" className={`${inputCls} mt-1 text-right  num`} value={custom.amount} onChange={(e) => setCustom({ ...custom, amount: e.target.value })} />
            </label>
            <label className="block">
              <span className="text-[13px] font-medium text-ink-soft">Paid on</span>
              <input type="date" className={`${inputCls} mt-1  num`} value={custom.date} onChange={(e) => setCustom({ ...custom, date: e.target.value })} />
            </label>
            <label className="block">
              <span className="text-[13px] font-medium text-ink-soft">Currency</span>
              <input maxLength={3} className={`${inputCls} mt-1 num uppercase`} value={custom.currency} onChange={(e) => setCustom({ ...custom, currency: e.target.value.toUpperCase() })} />
            </label>
            {customError && <p className="col-span-3 text-[13px] text-bad">{customError}</p>}
          </div>
        )}

        <label className="block">
          <span className="text-[13px] font-medium text-ink-soft">Paid from (sandbox personal account)</span>
          <input type="email" className={`${inputCls} mt-1`} value={payer} onChange={(e) => setPayer(e.target.value)} />
        </label>

        <Button className="w-full" onClick={run} disabled={busy || !row || Boolean(customError)} data-testid="sim-run">
          {busy ? (
            <>
              <Spinner /> Paying through PayPal…
            </>
          ) : (
            "Send payment"
          )}
        </Button>
        <p className="text-[12.5px] leading-relaxed text-ink-muted">
          This creates and captures a real order in the PayPal sandbox. No real money moves: the server refuses to run against anything but the sandbox.
        </p>
      </div>

      <div className="min-w-0 lg:pt-1">
        <h2 className="text-[17px] font-semibold text-ink">What happened</h2>
        <div className="mt-4">
          {result ? (
            <SimulationResult res={result.res} row={result.row} />
          ) : (
            <p className="max-w-[60ch] text-[14px] leading-relaxed text-ink-muted">
              Choose what the client pays and send it. You will see PayPal capture the money, the audit against the contract, Leash's decision and, if Leash allows it, the correction invoice it sends.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

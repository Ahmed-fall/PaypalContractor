import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { isApiError } from "../../api/client";
import { Button } from "../common/Button";
import { Card, PageHeader } from "../common/Card";
import { Modal } from "../common/Modal";
import { DecisionBadge, PayrollBadge, Pill } from "../common/StatusBadge";
import { centsToString, formatMoney } from "../../lib/money";
import type { OwnerAction, PayrollRunOut, PayrollSendResponse } from "../../types/contracts";
import { activePolicyQuiet, ledgerQuiet, payrollRunQuiet, sendPayrollQuiet, updatePayrollLineQuiet } from "./api";
import { flagLabel } from "./format";
import PayrollRunGrid, { type RunGridRow } from "./PayrollRunGrid";
import {
  approvableRunFlags,
  canApprove,
  isLocked,
  parseOpenAsks,
  payBlockers,
  peopleLabel,
  runTotalsCents,
  sendKey,
} from "./run";

const POLL_MS = 3000;
const SOMETHING_WRONG = "Something went wrong. Try again.";
const UNAVAILABLE = "PayPal didn't confirm. The batch may still go through; this page will keep checking.";

const message = (e: unknown) => (isApiError(e) ? e.message : SOMETHING_WRONG);

function Stat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-[12px] font-medium uppercase tracking-[0.04em] text-ink-muted">{label}</dt>
      <dd className="mt-1 text-[18px] font-semibold text-ink">{children}</dd>
    </div>
  );
}

/** One payroll run: review Leash's checks, approve or exclude lines, pay the team, watch PayPal. */
export default function PayrollRunPage() {
  const { runId = "" } = useParams();
  const live = useRef(true);

  const [run, setRun] = useState<PayrollRunOut | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [milestoneCode, setMilestoneCode] = useState<string | null>(null);
  const [limitPct, setLimitPct] = useState("80");

  const [ticked, setTicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [editError, setEditError] = useState<string | null>(null);

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const [sent, setSent] = useState<PayrollSendResponse | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [highlight, setHighlight] = useState<{ lineIds: string[]; runFlags: string[] } | null>(null);
  const [pollFailed, setPollFailed] = useState(false);

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const refetch = useCallback(async () => {
    try {
      const next = await payrollRunQuiet(runId);
      if (live.current) {
        setRun(next);
        setLoadError(null);
      }
    } catch (e) {
      if (live.current) setLoadError(message(e));
    }
  }, [runId]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  const milestoneId = run?.milestone_id ?? null;
  useEffect(() => {
    if (!milestoneId) return;
    let on = true;
    ledgerQuiet()
      .then((res) => on && setMilestoneCode(res.rows.find((r) => r.milestone_id === milestoneId)?.milestone_code ?? null))
      .catch(() => undefined); // the id is shown instead
    activePolicyQuiet()
      .then((p) => on && setLimitPct(p.payroll_margin_limit_pct))
      .catch(() => undefined); // the default limit is 80%
    return () => {
      on = false;
    };
  }, [milestoneId]);

  // Poll every 3 s while PayPal works on the batch; stop on PAID/PARTIAL/FAILED and on unmount.
  const status = run?.status;
  useEffect(() => {
    if (status !== "SENT" || !runId) return;
    let inFlight = false;
    const id = setInterval(() => {
      if (inFlight) return;
      inFlight = true;
      payrollRunQuiet(runId)
        .then((next) => {
          if (!live.current) return;
          setRun(next);
          setPollFailed(false);
        })
        .catch(() => live.current && setPollFailed(true))
        .finally(() => {
          inFlight = false;
        });
    }, POLL_MS);
    return () => clearInterval(id);
  }, [status, runId]);

  const onAction = useCallback(
    async (lineId: string, action: OwnerAction) => {
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy(true);
      setRowErrors({});
      setEditError(null);
      try {
        const next = await updatePayrollLineQuiet(runId, lineId, { owner_action: action });
        if (!live.current) return;
        setRun(next); // the server re-checks the whole run: replace it, never patch locally
        setHighlight(null);
        setSendError(null);
      } catch (e) {
        if (!live.current) return;
        if (isApiError(e) && e.code === "RUN_NOT_SENDABLE") {
          setEditError(e.message);
          void refetch();
        } else {
          setRowErrors({ [lineId]: message(e) });
        }
      } finally {
        busyRef.current = false;
        if (live.current) setBusy(false);
      }
    },
    [runId, refetch],
  );
  const onGridAction = useCallback((lineId: string, action: OwnerAction) => void onAction(lineId, action), [onAction]);

  const locked = run ? isLocked(run.status) : true;
  const tickable = useMemo(() => (run ? approvableRunFlags(run) : []), [run]);
  const approved = useMemo(() => tickable.filter((f) => ticked.includes(f)), [tickable, ticked]);
  const blockers = run ? payBlockers(run, approved, limitPct) : [];
  const totals = run ? runTotalsCents(run) : null;

  const rows = useMemo<RunGridRow[]>(
    () =>
      run
        ? run.lines.map((l) => ({
            ...l,
            attention: highlight?.lineIds.includes(l.line_id) ?? false,
            locked: isLocked(run.status),
            approvable: canApprove(l, run.status),
            busy,
            error: rowErrors[l.line_id] ?? null,
          }))
        : [],
    [run, highlight, busy, rowErrors],
  );

  const send = async () => {
    if (!run || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    setSendError(null);
    setUnavailable(false);
    setHighlight(null);
    try {
      const res = await sendPayrollQuiet(run.run_id, {
        confirm: true,
        approve_run_flags: approved,
        idempotency_key: sendKey(run.run_id),
      });
      if (!live.current) return;
      setSent(res);
    } catch (e) {
      if (!live.current) return;
      if (isApiError(e) && e.code === "LEASH_ASK_UNRESOLVED") {
        setHighlight(parseOpenAsks(e.details));
        setSendError(e.message);
      } else if (isApiError(e) && (e.code === "PAYPAL_UNAVAILABLE" || e.status === 503)) {
        setUnavailable(true); // the run is SENT with an unknown batch: polling picks it up
      } else {
        setSendError(message(e)); // RUN_NOT_SENDABLE, LEASH_DENIED, a lost answer: show, then re-read the run
      }
    } finally {
      sendingRef.current = false;
      if (live.current) {
        setSending(false);
        setConfirmOpen(false);
      }
    }
    if (live.current) await refetch();
  };

  const closeConfirm = useCallback(() => {
    if (!sendingRef.current) setConfirmOpen(false);
  }, []);

  if (!run) {
    return (
      <div>
        <PageHeader title="Payroll run" />
        {loadError ? (
          <p role="alert" className="border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
            Could not load this payroll run. {loadError}
          </p>
        ) : (
          <p className="text-[14px] text-ink-soft">Loading the payroll run...</p>
        )}
        <Link to="/leash/payroll" className="mt-4 inline-block text-[14px] font-medium text-ink underline">
          Back to payroll
        </Link>
      </div>
    );
  }

  const payTotal = formatMoney(centsToString(totals?.payCents ?? 0));
  const highlightLines = highlight ? run.lines.filter((l) => highlight.lineIds.includes(l.line_id)) : [];
  const paidTotal = formatMoney(centsToString(totals?.paidCents ?? 0));

  return (
    <div>
      <PageHeader
        title={`Payroll run ${milestoneCode ?? run.milestone_id ?? run.period ?? ""}`.trim()}
        description="Leash checked every line. Approve or exclude what it asks about, then pay the team in one PayPal batch."
        meta={
          <>
            <PayrollBadge status={run.status} />
            <Link to="/leash/payroll" className="font-medium text-ink underline">
              All runs
            </Link>
          </>
        }
      />

      <div className="flex flex-col gap-6">
        <Card title="Summary">
          <dl className="grid gap-5 sm:grid-cols-4">
            <Stat label="Total">
              <span className="num">{formatMoney(run.total)}</span>
            </Stat>
            <Stat label="Cash in">
              <span className="num">{formatMoney(run.cash_in)}</span>
            </Stat>
            <Stat label="Cash used">
              <span className="num">{run.cash_used_pct}%</span>
              <span className="ml-2 text-[13px] font-normal text-ink-muted">limit {limitPct}%</span>
            </Stat>
            <Stat label="Leash">
              <span className="flex flex-wrap items-center gap-1">
                <DecisionBadge decision={run.run_decision} />
                {run.run_flags.map((f) => (
                  <Pill key={f} tone="warn" title={f}>
                    {flagLabel(f)}
                  </Pill>
                ))}
              </span>
            </Stat>
          </dl>
        </Card>

        {locked ? (
          <StatusPanel
            run={run}
            sent={sent}
            unavailable={unavailable}
            pollFailed={pollFailed}
            paidPeople={totals?.paidPeople ?? 0}
            sentPeople={totals?.sentPeople ?? 0}
            paidTotal={paidTotal}
          />
        ) : (
          <Card title="Pay the team">
            {tickable.length > 0 && (
              <fieldset className="mb-4">
                <legend className="mb-2 text-[13px] font-medium text-ink">Run checks to approve</legend>
                {tickable.map((f) => (
                  <label key={f} className="flex items-center gap-2 text-[14px] text-ink">
                    <input
                      type="checkbox"
                      className="h-4 w-4"
                      checked={ticked.includes(f)}
                      disabled={sending}
                      onChange={(e) => setTicked((prev) => (e.target.checked ? [...prev, f] : prev.filter((x) => x !== f)))}
                    />
                    <span>Approve: {flagLabel(f)}</span>
                  </label>
                ))}
              </fieldset>
            )}
            <div className="flex flex-wrap items-center gap-4">
              <Button
                onClick={() => setConfirmOpen(true)}
                disabled={blockers.length > 0 || sending || busy}
                aria-describedby={blockers.length > 0 ? "pay-blockers" : undefined}
              >
                Pay team
              </Button>
              {blockers.length > 0 && (
                <p id="pay-blockers" className="text-[14px] text-ink-soft">
                  {blockers.join(". ")}.
                </p>
              )}
            </div>
            {highlight && (highlightLines.length > 0 || highlight.runFlags.length > 0) && (
              <div role="alert" aria-label="Still needs your decision" className="mt-4 border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
                <p>{sendError ?? "Some checks still need your decision."}</p>
                <ul className="mt-1 list-disc pl-5">
                  {highlightLines.map((l) => (
                    <li key={l.line_id}>
                      {l.member_name}: {l.flags.map(flagLabel).join(", ") || "needs your decision"}
                    </li>
                  ))}
                  {highlight.runFlags.map((f) => (
                    <li key={f}>Approve: {flagLabel(f)}</li>
                  ))}
                </ul>
              </div>
            )}
            {unavailable && (
              <p role="status" className="mt-4 text-[14px] text-warn">
                {UNAVAILABLE}
              </p>
            )}
            {sendError && !highlight && (
              <p role="alert" className="mt-4 border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
                {sendError}
              </p>
            )}
          </Card>
        )}

        {editError && (
          <p role="alert" className="border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
            {editError}
          </p>
        )}

        <Card title="Lines">
          <PayrollRunGrid rows={rows} onAction={onGridAction} />
        </Card>
      </div>

      <Modal
        open={confirmOpen}
        title="Pay the team"
        onClose={closeConfirm}
        width="sm"
        footer={
          <>
            <Button variant="secondary" onClick={closeConfirm} disabled={sending}>
              Cancel
            </Button>
            <Button onClick={() => void send()} disabled={sending} data-autofocus>
              {sending ? "Sending..." : "Send payment"}
            </Button>
          </>
        }
      >
        <p className="text-[15px] text-ink">
          Send {payTotal} to {peopleLabel(totals?.people ?? 0)} through PayPal Payouts? This can't be undone.
        </p>
      </Modal>
    </div>
  );
}

function StatusPanel({
  run,
  sent,
  unavailable,
  pollFailed,
  paidPeople,
  sentPeople,
  paidTotal,
}: {
  run: PayrollRunOut;
  sent: PayrollSendResponse | null;
  unavailable: boolean;
  pollFailed: boolean;
  paidPeople: number;
  sentPeople: number;
  paidTotal: string;
}) {
  let banner: ReactNode;
  if (run.status === "PAID") {
    banner = <p className="font-semibold text-good">{`Paid: ${peopleLabel(paidPeople)}, ${paidTotal}`}</p>;
  } else if (run.status === "PARTIAL") {
    banner = (
      <p className="font-semibold text-warn">{`Partly paid: ${paidPeople} of ${peopleLabel(sentPeople)} paid, ${paidTotal}. Check the lines below.`}</p>
    );
  } else if (run.status === "FAILED") {
    banner = <p className="font-semibold text-bad">PayPal did not pay this run. Check the lines below.</p>;
  } else {
    banner = <p className="text-ink">Checking PayPal every few seconds.</p>;
  }
  return (
    <Card title="PayPal">
      <div role="status" className="flex flex-col gap-2 text-[14px]">
        {sent ? (
          <p className="text-ink">{`Sent to PayPal. Batch ${sent.paypal_batch_id}`}</p>
        ) : (
          <p className="text-ink">
            Sent to PayPal{run.sender_batch_id ? <span className="num">{` (${run.sender_batch_id})`}</span> : null}.
          </p>
        )}
        {unavailable && <p className="text-warn">{UNAVAILABLE}</p>}
        {banner}
        {pollFailed && <p className="text-[13px] text-ink-muted">Could not refresh just now. Still trying.</p>}
      </div>
    </Card>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { isApiError } from "../../api/client";
import { Button } from "../common/Button";
import { Card, PageHeader } from "../common/Card";
import { Modal } from "../common/Modal";
import { DecisionBadge, PayrollBadge, Pill } from "../common/StatusBadge";
import { centsToString, formatMoney, toCents } from "../../lib/money";
import type { OwnerAction, PayrollRunOut, PayrollSendResponse } from "../../types/contracts";
import { activePolicyQuiet, ledgerQuiet, payrollRunQuiet, sendPayrollQuiet, updatePayrollLineQuiet } from "./api";
import { errorText } from "./format";
import PayrollRunGrid, { type RunGridRow } from "./PayrollRunGrid";
import {
  approvableRunFlags,
  canApprove,
  isLocked,
  isPayable,
  needsRecheck,
  parseOpenAsks,
  payBlockers,
  peopleLabel,
  runFlagLabel,
  runTotalsCents,
  sendKey,
} from "./run";

const POLL_MS = 3000;
const UNAVAILABLE = "PayPal didn't confirm. The batch may still go through; this page will keep checking.";
const NOT_SENT = "PayPal didn't get it. You can try again.";

const message = (e: unknown) => errorText(e);

function Stat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-[12px] font-medium uppercase tracking-[0.04em] text-ink-muted">{label}</dt>
      <dd className="mt-1 text-[18px] font-semibold text-ink">{children}</dd>
    </div>
  );
}

/**
 * `/leash/payroll/:runId`. Keyed by the run id, so moving to another run (same route, no
 * unmount) starts from clean state: no ticked flags, no send result, no poller carried over.
 */
export default function PayrollRunPage() {
  const { runId = "" } = useParams();
  return <RunReview key={runId} runId={runId} />;
}

type SendFailure = "asks" | "refused" | "unavailable" | "unknown";

/** One payroll run: review Leash's checks, approve or exclude lines, pay the team, watch PayPal. */
function RunReview({ runId }: { runId: string }) {
  const live = useRef(true);
  const runRef = useRef<PayrollRunOut | null>(null);

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
  const [stale, setStale] = useState(false);
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

  /**
   * Take a run from the server. Answers for another run are dropped. Ticked run flags only
   * hold for the total and flags the owner saw, so they are cleared when either changes.
   */
  const accept = useCallback(
    (next: PayrollRunOut, clearTicks = false): boolean => {
      if (!live.current || next.run_id !== runId) return false;
      const prev = runRef.current;
      if (clearTicks || !prev || prev.total !== next.total || prev.run_flags.join("|") !== next.run_flags.join("|")) {
        setTicked([]);
      }
      runRef.current = next;
      setRun(next);
      return true;
    },
    [runId],
  );

  const refetch = useCallback(async (): Promise<PayrollRunOut | null> => {
    try {
      const next = await payrollRunQuiet(runId);
      if (!accept(next)) return null;
      setLoadError(null);
      return next;
    } catch (e) {
      if (live.current) setLoadError(message(e));
      return null;
    }
  }, [runId, accept]);

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
          if (accept(next)) setPollFailed(false);
        })
        .catch(() => live.current && setPollFailed(true))
        .finally(() => {
          inFlight = false;
        });
    }, POLL_MS);
    return () => clearInterval(id);
  }, [status, runId, accept]);

  const onAction = useCallback(
    async (lineId: string, action: OwnerAction) => {
      if (busyRef.current || sendingRef.current) return;
      busyRef.current = true;
      setBusy(true);
      setRowErrors({});
      setEditError(null);
      try {
        const next = await updatePayrollLineQuiet(runId, lineId, { owner_action: action });
        // The server re-checks the whole run: replace it, never patch locally. Any edit
        // re-opens the run flags on the server (approved_at = None), so untick them here too.
        if (!accept(next, true)) return;
        setHighlight(null);
        setSendError(null);
        setStale(false);
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
    [runId, accept, refetch],
  );
  const onGridAction = useCallback((lineId: string, action: OwnerAction) => void onAction(lineId, action), [onAction]);

  const locked = run ? isLocked(run.status) : true;
  const tickable = useMemo(() => (run ? approvableRunFlags(run).filter((f) => f !== "DEFAULT") : []), [run]);
  const approved = useMemo(() => tickable.filter((f) => ticked.includes(f)), [tickable, ticked]);
  const blockers = run ? payBlockers(run, approved, limitPct) : [];
  const totals = run ? runTotalsCents(run) : null;
  // busy/sending guard against double actions. confirmOpen is defense in depth: the Modal already
  // makes the grid inert, but the dialog quotes run.total, so rows must not change under it (I1).
  const rowsBusy = busy || sending || confirmOpen;

  const rows = useMemo<RunGridRow[]>(
    () =>
      run
        ? run.lines.map((l) => ({
            ...l,
            attention: highlight?.lineIds.includes(l.line_id) ?? false,
            locked: isLocked(run.status),
            approvable: canApprove(l, run.status),
            busy: rowsBusy,
            error: rowErrors[l.line_id] ?? null,
          }))
        : [],
    [run, highlight, rowsBusy, rowErrors],
  );

  /** The modal states the server's total; refuse to open it when the lines on screen add up to something else. */
  const openConfirm = () => {
    if (!run || !totals || blockers.length > 0 || busyRef.current) return;
    if (toCents(run.total) !== totals.payCents) {
      setStale(true);
      return;
    }
    setStale(false);
    setSendError(null);
    setConfirmOpen(true);
  };

  const refreshStale = async () => {
    setStale(false);
    await refetch();
  };

  const send = async () => {
    const current = runRef.current;
    if (!current || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    setSendError(null);
    setUnavailable(false);
    setHighlight(null);
    let failure: SendFailure | null = null;
    let failureText = "";
    try {
      const res = await sendPayrollQuiet(
        current.run_id,
        { confirm: true, approve_run_flags: approved, idempotency_key: sendKey(current.run_id) },
        current.total, // the server refuses (409) if the re-checked run would pay anything else
      );
      if (!live.current) return;
      setSent(res);
      // 202: PayPal holds the batch and its items are PENDING. Lock the screen and start
      // polling now; nothing here waits on the re-read below succeeding.
      const base = runRef.current ?? current;
      accept({
        ...base,
        status: "SENT",
        sender_batch_id: res.sender_batch_id,
        lines: base.lines.map((l) => (isPayable(l) && l.payout_status === null ? { ...l, payout_status: "PENDING" } : l)),
      });
    } catch (e) {
      if (!live.current) return;
      if (isApiError(e) && e.code === "LEASH_ASK_UNRESOLVED") {
        failure = "asks";
        setHighlight(parseOpenAsks(e.details));
        setSendError(e.message);
      } else if (isApiError(e) && (e.code === "RUN_NOT_SENDABLE" || e.code === "LEASH_DENIED")) {
        failure = "refused";
        setSendError(e.message);
      } else if (isApiError(e) && e.code === "PAYPAL_UNAVAILABLE") {
        failure = "unavailable"; // the server marked the run SENT with an unknown batch
        setUnavailable(true);
      } else {
        failure = "unknown"; // a lost answer, any other 5xx: the run is re-read below
        failureText = message(e);
        setSendError(failureText);
      }
    } finally {
      sendingRef.current = false;
      if (live.current) {
        setSending(false);
        setConfirmOpen(false);
      }
    }
    if (!live.current) return;
    const fresh = await refetch();
    if (!live.current || !fresh || isLocked(fresh.status)) return;
    // The server holds no batch for this run, so nothing went out.
    if (failure === "unavailable") {
      setUnavailable(false);
      setSendError(NOT_SENT);
    } else if (failure === "unknown") {
      setSendError(`${failureText} ${NOT_SENT}`);
    }
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

  const highlightLines = highlight ? run.lines.filter((l) => highlight.lineIds.includes(l.line_id)) : [];
  const paidTotal = formatMoney(centsToString(totals?.paidCents ?? 0));
  const unclaimed = run.lines.filter((l) => l.payout_status === "UNCLAIMED").map((l) => l.member_name);

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
                  <Pill key={f} tone={f === "DEFAULT" ? "bad" : "warn"} title={f}>
                    {runFlagLabel(f, limitPct)}
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
            unclaimed={unclaimed}
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
                    <span>Approve: {runFlagLabel(f, limitPct)}</span>
                  </label>
                ))}
              </fieldset>
            )}
            {needsRecheck(run) && (
              <p className="mb-4 text-[14px] text-ink-soft">
                Leash couldn&apos;t check this run yet. Pay team asks Leash to check it again first, and stops if anything still needs you.
              </p>
            )}
            <div className="flex flex-wrap items-center gap-4">
              <Button
                onClick={openConfirm}
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
            {stale && (
              <div role="alert" className="mt-4 flex flex-wrap items-center gap-3 border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
                <span>The server&apos;s total doesn&apos;t match these lines.</span>
                <Button size="sm" variant="secondary" onClick={() => void refreshStale()}>
                  Refresh the run
                </Button>
              </div>
            )}
            {highlight && (highlightLines.length > 0 || highlight.runFlags.length > 0) && (
              <div role="alert" aria-label="Still needs your decision" className="mt-4 border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
                <p>{sendError ?? "Some checks still need your decision."}</p>
                <ul className="mt-1 list-disc pl-5">
                  {highlightLines.map((l) => (
                    <li key={l.line_id}>
                      {l.member_name}: {l.flags.map((f) => runFlagLabel(f, limitPct)).join(", ") || "needs your decision"}
                    </li>
                  ))}
                  {highlight.runFlags.map((f) => (
                    <li key={f}>{f === "DEFAULT" ? runFlagLabel(f) : `Approve: ${runFlagLabel(f, limitPct)}`}</li>
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
          Send {formatMoney(run.total)} to {peopleLabel(totals?.people ?? 0)} through PayPal Payouts? This can&apos;t be undone.
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
  unclaimed,
}: {
  run: PayrollRunOut;
  sent: PayrollSendResponse | null;
  unavailable: boolean;
  pollFailed: boolean;
  paidPeople: number;
  sentPeople: number;
  paidTotal: string;
  unclaimed: string[];
}) {
  const waiting = unclaimed.map((n) => `${n} hasn't claimed it yet.`);
  let banner: ReactNode;
  if (run.status === "PAID") {
    banner = <p className="font-semibold text-good">{`Paid: ${peopleLabel(paidPeople)}, ${paidTotal}`}</p>;
  } else if (run.status === "PARTIAL") {
    const text = [`Partly paid: ${paidPeople} of ${peopleLabel(sentPeople)} paid, ${paidTotal}.`, ...waiting, "Check the lines below."];
    banner = <p className="font-semibold text-warn">{text.join(" ")}</p>;
  } else if (run.status === "FAILED") {
    banner = <p className="font-semibold text-bad">PayPal did not pay this run. Check the lines below.</p>;
  } else {
    banner = <p className="text-ink">{["Checking PayPal every few seconds.", ...waiting].join(" ")}</p>;
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

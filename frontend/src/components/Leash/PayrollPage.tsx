import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AgGridReact, type CustomCellRendererProps } from "ag-grid-react";
import type { ColDef, GetRowIdParams } from "ag-grid-community";
import { isApiError } from "../../api/client";
import { Button } from "../common/Button";
import { Card, PageHeader } from "../common/Card";
import { Pill } from "../common/StatusBadge";
import { formatMoney, toCents } from "../../lib/money";
import type {
  LedgerRow,
  PayrollRunOut,
  PayrollRunStatus,
  TimesheetField,
  TimesheetMappingRequired,
  TimesheetParsed,
} from "../../types/contracts";
import { confirmTimesheetMappingQuiet, createPayrollRunQuiet, discardTimesheet, ledgerQuiet, listPayrollRuns, uploadTimesheetQuiet } from "./api";
import MappingDialog from "./MappingDialog";

const MAX_BYTES = 2 * 1024 * 1024;
const TOO_BIG = "This timesheet is larger than 2 MB.";
const SOMETHING_WRONG = "Something went wrong. Try again.";
const fileInput =
  "block w-full text-[14px] text-ink file:mr-3 file:h-10 file:rounded file:border file:border-paper-line file:bg-white file:px-4 file:text-[14px]";
const selectCls = "h-10 w-full rounded border border-paper-line bg-white px-2 text-[14px] outline-none focus:border-ink";

interface UploadProblem {
  message: string;
  rows: Array<{ row: number; error: string }>;
}

/** 413/415/422/409 from the upload routes, in words, with the bad rows when the server lists them (details.rows). */
function uploadProblem(e: unknown): UploadProblem {
  if (!isApiError(e)) return { message: SOMETHING_WRONG, rows: [] };
  const raw = (e.details as { rows?: unknown } | null)?.rows;
  const rows: UploadProblem["rows"] = [];
  if (Array.isArray(raw)) {
    for (const item of raw as Array<{ row?: unknown; error?: unknown }>) {
      if (typeof item?.row === "number" && typeof item?.error === "string") rows.push({ row: item.row, error: item.error });
    }
  }
  return { message: e.message, rows };
}

/** An earlier upload the server says is in the way, and where the owner saw that. */
interface StaleUpload {
  id: string;
  where: "upload" | "prepare";
}

function uploadIdOf(e: unknown): string | null {
  if (!isApiError(e)) return null;
  const id = (e.details as { upload_id?: unknown } | null)?.upload_id;
  return typeof id === "string" && id ? id : null;
}

/** The upload is gone or no longer waiting for a mapping: nothing to confirm or discard any more. */
function uploadGone(e: unknown): boolean {
  return isApiError(e) && (e.status === 404 || e.code === "UPLOAD_NOT_PENDING" || e.code === "NOT_DISCARDABLE");
}

function runTone(status: PayrollRunStatus): "good" | "warn" | "bad" | "neutral" {
  if (status === "PAID") return "good";
  if (status === "FAILED") return "bad";
  if (status === "AWAITING_APPROVAL" || status === "PARTIAL") return "warn";
  return "neutral";
}

const STATUS_LABEL: Record<PayrollRunStatus, string> = {
  PREPARED: "Prepared",
  CHECKED: "Checked",
  AWAITING_APPROVAL: "Needs your approval",
  SENT: "Sent",
  PAID: "Paid",
  PARTIAL: "Partly paid",
  FAILED: "Failed",
};

function describeMilestone(r: LedgerRow): string {
  return `${r.milestone_code} - ${r.milestone_title} (${r.contract_title})`;
}

interface GridContext {
  codeFor: (milestoneId: string | null) => string;
}
type RunCell = CustomCellRendererProps<PayrollRunOut, unknown, GridContext>;

/** Upload hours, prepare a payroll run for a paid milestone, and see past runs. */
export default function PayrollPage() {
  const navigate = useNavigate();
  const fileRef = useRef<HTMLInputElement>(null);

  const [uploading, setUploading] = useState(false);
  const [problem, setProblem] = useState<UploadProblem | null>(null);
  const [parsed, setParsed] = useState<TimesheetParsed | null>(null);
  const [pending, setPending] = useState<TimesheetMappingRequired | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [mapBusy, setMapBusy] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);
  const [discarded, setDiscarded] = useState<string | null>(null);
  const [stale, setStale] = useState<StaleUpload | null>(null);
  const confirming = useRef(false);
  const preparingRef = useRef(false);

  const [milestones, setMilestones] = useState<LedgerRow[]>([]);
  const [milestonesFailed, setMilestonesFailed] = useState(false);
  const [milestoneId, setMilestoneId] = useState("");
  const [preparing, setPreparing] = useState(false);
  const [prepareError, setPrepareError] = useState<string | null>(null);

  const [runs, setRuns] = useState<PayrollRunOut[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsFailed, setRunsFailed] = useState(false);

  useEffect(() => {
    let live = true;
    ledgerQuiet()
      .then((res) => live && setMilestones(res.rows))
      .catch(() => live && setMilestonesFailed(true));
    listPayrollRuns()
      .then((rows) => live && setRuns(rows))
      .catch(() => live && setRunsFailed(true))
      .finally(() => live && setRunsLoading(false));
    return () => {
      live = false;
    };
  }, []);

  const clearResult = () => {
    setProblem(null);
    setParsed(null);
    setPending(null);
    setDialogOpen(false);
    setMapError(null);
    setDiscarded(null);
    setStale(null);
    setPrepareError(null);
  };

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    clearResult(); // whatever the last file said no longer applies
    if (!file) return;
    if (file.size > MAX_BYTES) {
      setProblem({ message: TOO_BIG, rows: [] });
      e.target.value = "";
      return;
    }
    setUploading(true);
    try {
      const res = await uploadTimesheetQuiet(file);
      if (res.status === "PARSED") setParsed(res);
      else {
        setPending(res);
        setDialogOpen(true);
      }
    } catch (err) {
      setProblem(uploadProblem(err));
      const id = isApiError(err) && err.code === "DUPLICATE_UPLOAD" ? uploadIdOf(err) : null;
      if (id) setStale({ id, where: "upload" });
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = ""; // so the same file can be picked again
    }
  };

  const confirmMapping = async (mapping: Record<string, TimesheetField>) => {
    if (!pending || confirming.current) return;
    confirming.current = true;
    setMapBusy(true);
    setMapError(null);
    try {
      const res = await confirmTimesheetMappingQuiet(pending.upload_id, { mapping });
      setParsed(res);
      setPending(null);
      setDialogOpen(false);
    } catch (err) {
      if (uploadGone(err)) {
        setPending(null);
        setDialogOpen(false);
        setProblem({ message: "That timesheet is no longer waiting for a column mapping. Upload it again.", rows: [] });
        return;
      }
      const p = uploadProblem(err);
      setMapError(p.rows.length ? `${p.message} ${p.rows.map((r) => `Row ${r.row}: ${r.error}.`).join(" ")}` : p.message);
    } finally {
      confirming.current = false;
      setMapBusy(false);
    }
  };

  const discard = async () => {
    if (!pending) return;
    setMapBusy(true);
    setMapError(null);
    try {
      await discardTimesheet(pending.upload_id);
      setPending(null);
      setDialogOpen(false);
      setDiscarded("Upload discarded. Choose another file.");
    } catch (err) {
      if (uploadGone(err)) {
        setPending(null);
        setDialogOpen(false);
        setProblem({ message: "That timesheet is no longer waiting for a column mapping. Upload it again.", rows: [] });
        return;
      }
      setMapError(uploadProblem(err).message);
    } finally {
      setMapBusy(false);
    }
  };

  const prepare = async () => {
    if (!milestoneId || preparingRef.current) return;
    preparingRef.current = true;
    setPreparing(true);
    setPrepareError(null);
    try {
      const run = await createPayrollRunQuiet({ mode: "MILESTONE", milestone_id: milestoneId });
      navigate(`/leash/payroll/${encodeURIComponent(run.run_id)}`);
    } catch (err) {
      if (isApiError(err) && err.code === "MAPPING_REQUIRED") {
        setPrepareError("Finish or discard the pending timesheet first.");
        const id = uploadIdOf(err);
        if (id) setStale({ id, where: "prepare" });
      } else if (isApiError(err) && err.status === 404) setPrepareError("No timesheet hours for this milestone yet.");
      else setPrepareError(isApiError(err) ? err.message : SOMETHING_WRONG);
      preparingRef.current = false;
      setPreparing(false);
    }
  };

  /** Throw away the earlier upload the server named, then let the owner try again. */
  const discardStale = async () => {
    if (!stale) return;
    const { id, where } = stale;
    try {
      await discardTimesheet(id);
      setStale(null);
      setProblem(null);
      setPrepareError(null);
      setDiscarded(where === "upload" ? "Upload discarded. Choose another file." : "Upload discarded. You can prepare payroll again.");
    } catch (err) {
      setStale(null);
      const msg = uploadGone(err) ? "That timesheet is already in use." : uploadProblem(err).message;
      if (where === "upload") setProblem({ message: msg, rows: [] });
      else setPrepareError(msg);
    }
  };

  const codeFor = useCallback(
    (id: string | null) => milestones.find((m) => m.milestone_id === id)?.milestone_code ?? id ?? "",
    [milestones],
  );
  const context = useMemo<GridContext>(() => ({ codeFor }), [codeFor]);

  const colDefs = useMemo<ColDef<PayrollRunOut>[]>(
    () => [
      { colId: "milestone", headerName: "Milestone", width: 130, valueGetter: (p) => p.context.codeFor(p.data?.milestone_id ?? null) },
      {
        colId: "status",
        headerName: "Status",
        width: 190,
        valueGetter: (p) => (p.data ? STATUS_LABEL[p.data.status] : ""),
        cellRenderer: ({ data }: RunCell) => (data ? <Pill tone={runTone(data.status)}>{STATUS_LABEL[data.status]}</Pill> : null),
      },
      {
        colId: "total",
        headerName: "Total",
        width: 130,
        type: "rightAligned",
        headerClass: "ag-right-aligned-header",
        cellClass: "num",
        valueGetter: (p) => formatMoney(p.data?.total),
        comparator: (_a, _b, na, nb) => toCents(na.data?.total) - toCents(nb.data?.total),
      },
      {
        colId: "cash",
        headerName: "Cash used",
        width: 130,
        type: "rightAligned",
        headerClass: "ag-right-aligned-header",
        cellClass: "num",
        valueGetter: (p) => (p.data ? `${p.data.cash_used_pct}%` : ""),
        comparator: (_a, _b, na, nb) => toCents(na.data?.cash_used_pct) - toCents(nb.data?.cash_used_pct),
      },
      {
        colId: "batch",
        headerName: "PayPal batch",
        flex: 1,
        minWidth: 180,
        cellClass: "num",
        valueGetter: (p) => p.data?.sender_batch_id ?? "Not sent",
      },
      {
        colId: "open",
        headerName: "",
        width: 110,
        sortable: false,
        cellRenderer: ({ data }: RunCell) =>
          data ? (
            <Link
              to={`/leash/payroll/${encodeURIComponent(data.run_id)}`}
              aria-label={`Open run ${data.run_id}`}
              className="text-[14px] font-medium text-ink underline"
            >
              Open
            </Link>
          ) : null,
      },
    ],
    [],
  );
  const defaultColDef = useMemo<ColDef<PayrollRunOut>>(() => ({ sortable: true, resizable: true, suppressHeaderMenuButton: true }), []);

  const pendingMapping = pending !== null;

  return (
    <div>
      <PageHeader
        title="Payroll"
        description="Upload the hours, then prepare a payroll run for a milestone the client has paid. You approve before anything is sent."
      />

      <div className="flex flex-col gap-6">
        <Card title="Timesheet">
          <label htmlFor="timesheet-file" className="mb-1 block text-[13px] font-medium text-ink">
            Timesheet file
          </label>
          <input
            id="timesheet-file"
            ref={fileRef}
            type="file"
            accept=".csv,.xlsx"
            className={fileInput}
            onChange={(e) => void onFile(e)}
            disabled={uploading || pending !== null}
            aria-describedby={`timesheet-hint${pending ? " timesheet-pending" : ""}${problem ? " timesheet-problem" : ""}`}
            aria-invalid={problem ? true : undefined}
          />
          <p id="timesheet-hint" className="mt-1 text-[12px] text-ink-muted">
            CSV or XLSX, up to 2 MB and 500 rows.
          </p>

          {pending && (
            <p id="timesheet-pending" className="mt-1 text-[12px] text-ink-muted">
              Match or discard the pending timesheet first.
            </p>
          )}

          {uploading && <p className="mt-3 text-[14px] text-ink-soft">Reading the timesheet...</p>}

          {problem && (
            <div id="timesheet-problem" role="alert" className="mt-4 border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
              <p>{problem.message}</p>
              {problem.rows.length > 0 && (
                <ul aria-label="Rows to fix" className="mt-1 list-disc pl-5">
                  {problem.rows.map((r) => (
                    <li key={`${r.row}-${r.error}`}>
                      Row {r.row}: {r.error}
                    </li>
                  ))}
                </ul>
              )}
              {stale?.where === "upload" && (
                <Button size="sm" variant="secondary" className="mt-2" onClick={() => void discardStale()}>
                  Discard that upload
                </Button>
              )}
            </div>
          )}

          {parsed && (
            <div role="status" className="mt-4 text-[14px] text-ink">
              <p>
                {parsed.entries} rows for {parsed.milestones.join(", ") || "no milestone"}
              </p>
              {parsed.unknown_members.length > 0 && (
                <p className="mt-1 border-l-[3px] border-warn py-1 pl-3 text-warn">Not in your team: {parsed.unknown_members.join(", ")}</p>
              )}
              {parsed.warnings.map((w) => (
                <p key={w} className="mt-1 text-[13px] text-ink-soft">
                  {w}
                </p>
              ))}
            </div>
          )}

          {pendingMapping && !dialogOpen && (
            <div role="status" className="mt-4 flex flex-wrap items-center gap-3 text-[14px] text-ink">
              <span>This timesheet is waiting for you to match its columns.</span>
              <Button size="sm" variant="secondary" onClick={() => setDialogOpen(true)}>
                Match columns
              </Button>
            </div>
          )}
          {discarded && (
            <p role="status" className="mt-4 text-[14px] text-ink-soft">
              {discarded}
            </p>
          )}
        </Card>

        <Card title="Prepare payroll">
          <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
            <div>
              <label htmlFor="milestone-select" className="mb-1 block text-[13px] font-medium text-ink">
                Milestone
              </label>
              <select
                id="milestone-select"
                className={selectCls}
                value={milestoneId}
                onChange={(e) => {
                  setMilestoneId(e.target.value);
                  setPrepareError(null);
                }}
                aria-describedby={prepareError ? "prepare-error" : milestonesFailed ? "milestone-load-error" : undefined}
              >
                <option value="">Choose a milestone</option>
                {milestones.map((m) => (
                  <option key={m.milestone_id} value={m.milestone_id}>
                    {describeMilestone(m)}
                  </option>
                ))}
              </select>
            </div>
            <Button onClick={() => void prepare()} disabled={!milestoneId || preparing || pendingMapping}>
              Prepare payroll
            </Button>
          </div>
          {pendingMapping && <p className="mt-3 text-[13px] text-ink-muted">Match or discard the pending timesheet first.</p>}
          {milestonesFailed && (
            <p id="milestone-load-error" role="alert" className="mt-3 text-[13px] text-bad">
              Could not load your milestones. Reload the page.
            </p>
          )}
          {prepareError && (
            <div className="mt-4 flex flex-wrap items-center gap-3 border-l-[3px] border-bad py-1 pl-3">
              <p id="prepare-error" role="alert" className="text-[14px] text-bad">
                {prepareError}
              </p>
              {stale?.where === "prepare" && (
                <Button size="sm" variant="secondary" onClick={() => void discardStale()}>
                  Discard that upload
                </Button>
              )}
            </div>
          )}
        </Card>

        <Card title="Runs">
          {runsFailed && (
            <p role="alert" className="mb-3 text-[13px] text-bad">
              Could not load the payroll runs. Reload the page.
            </p>
          )}
          <div role="region" aria-label="Payroll runs" className="ag-theme-quartz fd-grid w-full">
            <AgGridReact<PayrollRunOut>
              rowData={runs}
              columnDefs={colDefs}
              defaultColDef={defaultColDef}
              context={context}
              getRowId={(p: GetRowIdParams<PayrollRunOut>) => p.data.run_id}
              domLayout="autoHeight"
              rowHeight={48}
              headerHeight={42}
              suppressColumnVirtualisation
              suppressCellFocus
              loading={runsLoading}
              overlayNoRowsTemplate={runsFailed ? "<span></span>" : "<span style='color:#6b7f7c;font-size:14px'>No payroll runs yet.</span>"}
            />
          </div>
        </Card>
      </div>

      {pending && (
        <MappingDialog
          key={pending.upload_id}
          open={dialogOpen}
          headers={pending.headers}
          proposal={pending.proposal}
          rowCount={pending.row_count}
          busy={mapBusy}
          error={mapError}
          onConfirm={(m) => void confirmMapping(m)}
          onDiscard={() => void discard()}
          onClose={() => setDialogOpen(false)}
        />
      )}
    </div>
  );
}

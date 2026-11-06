import { useCallback, useMemo, useRef } from "react";
import { AgGridReact, type CustomCellRendererProps } from "ag-grid-react";
import type { ColDef, GetRowIdParams } from "ag-grid-community";

const GRID_LABEL = "Payroll lines";
import { Button } from "../common/Button";
import { DecisionBadge, Pill } from "../common/StatusBadge";
import { formatMoney, toCents } from "../../lib/money";
import type { PayoutStatus, PayrollLineOut } from "../../types/contracts";
import { flagLabel, maskEmail, ownerActionLabel, payoutLabel } from "./format";
import { isOpenAsk } from "./run";

/** One grid row: the server's line plus what the screen may offer on it. */
export interface RunGridRow extends PayrollLineOut {
  /** The server's last 409 named this line as still open. */
  attention: boolean;
  /** The run was sent: no edits at all. */
  locked: boolean;
  /** `canApprove(line, run.status)`. */
  approvable: boolean;
  /** A PATCH for any line is in flight. */
  busy: boolean;
  /** A refusal for this line (e.g. 409 LINE_NOT_CHECKED), shown on the row. */
  error: string | null;
}

interface GridContext {
  onAction: (lineId: string, action: "APPROVED" | "EXCLUDED") => void;
}
type Cell = CustomCellRendererProps<RunGridRow, unknown, GridContext>;

function payoutTone(status: PayoutStatus | null): "good" | "warn" | "bad" | "neutral" {
  if (status === "SUCCESS") return "good";
  if (status === "FAILED") return "bad";
  if (status === "PENDING" || status === "UNCLAIMED") return "warn";
  return "neutral";
}

function FlagsCell({ data }: Cell) {
  if (!data || data.flags.length === 0) return null;
  return (
    <span className="flex flex-wrap gap-1 py-1">
      {data.flags.map((f) => (
        <Pill key={f} tone={f === "DEFAULT" ? "bad" : "warn"} title={f}>
          {flagLabel(f)}
        </Pill>
      ))}
    </span>
  );
}

function OwnerCell({ data }: Cell) {
  if (!data) return null;
  // The server's last 409 named this line: say so even if the screen thought it was settled.
  if (!data.locked && data.attention) return <span className="text-[13px] font-semibold text-bad">Needs your decision</span>;
  if (data.owner_action) return <span className="text-[13px] text-ink">{ownerActionLabel(data.owner_action)}</span>;
  if (!data.locked && isOpenAsk(data)) return <span className="text-[13px] text-ink-soft">Waiting for you</span>;
  return null;
}

function ActionsCell({ data, context }: Cell) {
  if (!data || data.locked) return null;
  const name = data.member_name;
  const act = (action: "APPROVED" | "EXCLUDED") => context.onAction(data.line_id, action);
  const approveButton = (
    <Button size="sm" aria-label={`Approve ${name}`} disabled={data.busy} onClick={() => act("APPROVED")}>
      Approve
    </Button>
  );
  let buttons: JSX.Element | null;
  if (data.owner_action === "EXCLUDED") {
    // "Undo" would send APPROVED. That is only harmless where Leash allowed the line; an
    // excluded ASK line comes back through an explicit Approve with its flags in view.
    const undo = data.decision === "ALLOW" && data.approvable;
    const reapprove = data.decision === "ASK" && data.approvable;
    buttons = (
      <span className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] text-ink-muted">Excluded</span>
        {undo && (
          <button
            type="button"
            className="text-[13px] font-medium text-ink underline disabled:opacity-45"
            aria-label={`Undo exclude ${name}`}
            disabled={data.busy}
            onClick={() => act("APPROVED")}
          >
            Undo
          </button>
        )}
        {reapprove && approveButton}
      </span>
    );
  } else {
    // Approve only where Leash asked and the server would accept it (canApprove).
    const showApprove = data.approvable && data.decision === "ASK" && data.owner_action === null;
    buttons = (
      <span className="flex flex-wrap items-center gap-2">
        {showApprove && approveButton}
        <Button size="sm" variant="secondary" aria-label={`Exclude ${name}`} disabled={data.busy} onClick={() => act("EXCLUDED")}>
          Exclude
        </Button>
      </span>
    );
  }
  return (
    <div className="flex flex-col gap-1 py-2">
      {buttons}
      {data.error && (
        <span role="alert" className="whitespace-normal text-[12px] leading-snug text-bad">
          {data.error}
        </span>
      )}
    </div>
  );
}

const money = (field: "rate" | "gross"): ColDef<RunGridRow> => ({
  colId: field,
  headerName: field === "rate" ? "Rate" : "Gross",
  width: 110,
  type: "rightAligned",
  headerClass: "ag-right-aligned-header",
  cellClass: "num",
  valueGetter: (p) => formatMoney(p.data?.[field]),
  comparator: (_a, _b, na, nb) => toCents(na.data?.[field]) - toCents(nb.data?.[field]),
});

/** The run's lines with Leash's decision, its reason, and the owner's Approve / Exclude. */
export default function PayrollRunGrid({
  rows,
  onAction,
}: {
  rows: RunGridRow[];
  onAction: GridContext["onAction"];
}) {
  const context = useMemo<GridContext>(() => ({ onAction }), [onAction]);

  const colDefs = useMemo<ColDef<RunGridRow>[]>(
    () => [
      {
        colId: "member",
        headerName: "Member",
        width: 130,
        valueGetter: (p) => p.data?.member_name ?? "",
        cellClassRules: { "fd-flag": (p) => Boolean(p.data && !p.data.locked && (p.data.attention || isOpenAsk(p.data))) },
      },
      { colId: "email", headerName: "Payout email", width: 220, valueGetter: (p) => (p.data ? maskEmail(p.data.payout_email) : "") },
      {
        colId: "hours",
        headerName: "Hours",
        width: 90,
        type: "rightAligned",
        headerClass: "ag-right-aligned-header",
        cellClass: "num",
        valueGetter: (p) => p.data?.hours ?? "",
      },
      money("rate"),
      money("gross"),
      {
        colId: "decision",
        headerName: "Leash",
        width: 150,
        valueGetter: (p) => p.data?.decision ?? "",
        cellRenderer: ({ data }: Cell) => (data ? <DecisionBadge decision={data.decision} /> : null),
      },
      {
        colId: "flags",
        headerName: "Checks",
        width: 220,
        wrapText: true,
        autoHeight: true,
        valueGetter: (p) => (p.data ? p.data.flags.join(" ") : ""),
        cellRenderer: FlagsCell,
      },
      {
        colId: "reason",
        headerName: "Why",
        flex: 1,
        minWidth: 240,
        wrapText: true,
        autoHeight: true,
        cellClass: "leading-snug py-2",
        valueGetter: (p) => p.data?.reason ?? "",
      },
      {
        colId: "owner",
        headerName: "Your decision",
        width: 170,
        valueGetter: (p) => (p.data ? `${p.data.owner_action ?? ""}|${p.data.attention}|${p.data.locked}` : ""),
        cellRenderer: OwnerCell,
      },
      {
        colId: "payout",
        headerName: "PayPal",
        valueGetter: (p) => (p.data ? payoutLabel(p.data.payout_status) : ""),
        width: 180,
        wrapText: true,
        autoHeight: true,
        cellRenderer: ({ data }: Cell) =>
          data ? (
            <span className="flex flex-col items-start gap-1 py-2">
              <Pill tone={payoutTone(data.payout_status)}>{payoutLabel(data.payout_status)}</Pill>
              {data.payout_status === "UNCLAIMED" && (
                <span className="whitespace-normal text-[12px] leading-snug text-warn">{`${data.member_name} hasn't claimed it yet`}</span>
              )}
            </span>
          ) : null,
      },
      {
        colId: "actions",
        headerName: "Actions",
        width: 230,
        sortable: false,
        autoHeight: true,
        valueGetter: (p) =>
          p.data
            ? [p.data.owner_action, p.data.locked, p.data.approvable, p.data.busy, p.data.error, p.data.decision].join("|")
            : "",
        cellRenderer: ActionsCell,
      },
    ],
    [],
  );
  const defaultColDef = useMemo<ColDef<RunGridRow>>(() => ({ sortable: true, resizable: true, suppressHeaderMenuButton: true }), []);

  // AG Grid renders its own role="grid" root; give it the same name as the region.
  const wrapper = useRef<HTMLDivElement>(null);
  const labelRoot = useCallback(() => {
    wrapper.current?.querySelector('[role="grid"], [role="treegrid"]')?.setAttribute("aria-label", GRID_LABEL);
  }, []);

  return (
    <div ref={wrapper} role="region" aria-label={GRID_LABEL} className="ag-theme-quartz fd-grid w-full">
      <AgGridReact<RunGridRow>
        rowData={rows}
        columnDefs={colDefs}
        defaultColDef={defaultColDef}
        context={context}
        getRowId={(p: GetRowIdParams<RunGridRow>) => p.data.line_id}
        domLayout="autoHeight"
        rowHeight={52}
        headerHeight={42}
        suppressColumnVirtualisation
        suppressCellFocus
        onGridReady={labelRoot}
        onFirstDataRendered={labelRoot}
        overlayNoRowsTemplate="<span style='color:#6b7f7c;font-size:14px'>This run has no lines.</span>"
      />
    </div>
  );
}

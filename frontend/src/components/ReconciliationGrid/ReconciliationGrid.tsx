import { useCallback, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, GetRowIdParams, GridApi, IsFullWidthRowParams, RowClassParams, RowHeightParams } from "ag-grid-community";
import type { LedgerRow, MarginResponse } from "../../types/contracts";
import { computeTotals, joinMargin, matchesStatus, type GridRow, type StatusFilter, type TotalsRow } from "./model";
import {
  ActionCell,
  CaptureCell,
  ClientCell,
  DetailRow,
  DueCell,
  ExpandCell,
  MarginCell,
  MilestoneCell,
  PayrollCell,
  StatusCell,
  isTotals,
  moneyCell,
  type GridContext,
  type Row,
} from "./cells";
import { COMPLIANCE_LABEL } from "../common/StatusBadge";
import { toCents } from "../../lib/money";
import { useMediaQuery } from "../../lib/useMediaQuery";

interface Props {
  rows: LedgerRow[];
  margin: MarginResponse | null;
  loading: boolean;
  onOpenRemediation: (discrepancyId: string) => void;
}

const FILTERS: { id: StatusFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "action", label: "Needs action" },
  { id: "owed", label: "Money owed" },
  { id: "sent", label: "Invoiced" },
  { id: "compliant", label: "Compliant" },
  { id: "unpaid", label: "Awaiting payment" },
];

const num = { cellClass: "num", headerClass: "ag-right-aligned-header", type: "rightAligned" } as const;

/**
 * The reconciliation ledger (plan WP-3.2 + WP-3.4): one row per milestone, money in
 * against the contract and money out to the team, with pinned totals in cents.
 * AG Grid Community only: the expandable detail is a full-width row, not Enterprise
 * master/detail, so there is no watermark.
 */
export function ReconciliationGrid({ rows, margin, loading, onOpenRemediation }: Props) {
  const apiRef = useRef<GridApi<Row> | null>(null);
  const [quick, setQuick] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [visibleTotals, setVisibleTotals] = useState<TotalsRow | null>(null);

  const joined = useMemo(() => joinMargin(rows, margin), [rows, margin]);
  // Pinned columns need room; on narrow screens AG Grid would drop them, so scroll instead.
  const wide = useMediaQuery("(min-width: 900px)");
  const left = wide ? ("left" as const) : null;
  const right = wide ? ("right" as const) : null;

  const rowData = useMemo<GridRow[]>(() => {
    const out: GridRow[] = [];
    for (const r of joined) {
      out.push(r);
      if (expanded.has(r.milestone_id)) out.push({ ...r, __key: `${r.milestone_id}__detail`, __detailFor: r.milestone_id });
    }
    return out;
  }, [joined, expanded]);

  const toggle = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const context = useMemo<GridContext>(() => ({ expanded, toggle, openRemediation: onOpenRemediation }), [expanded, toggle, onOpenRemediation]);

  const counts = useMemo(() => {
    const c: Record<StatusFilter, number> = { all: 0, action: 0, owed: 0, sent: 0, compliant: 0, unpaid: 0 };
    for (const r of joined) for (const f of FILTERS) if (matchesStatus(r, f.id)) c[f.id] += 1;
    return c;
  }, [joined]);

  const colDefs = useMemo<ColDef<Row>[]>(
    () => [
      { colId: "expand", headerName: "", width: 52, pinned: left, cellRenderer: ExpandCell, sortable: false, resizable: false, suppressMovable: true },
      {
        colId: "milestone",
        headerName: "Milestone",
        field: "milestone_code",
        pinned: left,
        width: 220,
        cellRenderer: MilestoneCell,
        getQuickFilterText: (p) => (p.data && !isTotals(p.data) ? `${p.data.milestone_code} ${p.data.milestone_title}` : ""),
      },
      {
        colId: "client",
        headerName: "Client & Contract",
        width: 250,
        valueGetter: (p) => (p.data && !isTotals(p.data) ? p.data.client_name : ""),
        cellRenderer: ClientCell,
        getQuickFilterText: (p) => (p.data && !isTotals(p.data) ? `${p.data.client_name} ${p.data.contract_title}` : ""),
      },
      { colId: "due", headerName: "Due Date", field: "due_date" as never, width: 130, cellRenderer: DueCell },
      { colId: "expected", headerName: "Expected", width: 128, ...num, valueGetter: (p) => p.data?.expected_amount, comparator: moneyCompare, cellRenderer: moneyCell("expected_amount") },
      { colId: "captured", headerName: "Captured", width: 128, ...num, valueGetter: (p) => p.data?.captured_amount, comparator: moneyCompare, cellRenderer: moneyCell("captured_amount") },
      {
        colId: "shortfall",
        headerName: "Shortfall",
        width: 120,
        ...num,
        valueGetter: (p) => p.data?.shortfall_amount,
        comparator: moneyCompare,
        cellRenderer: moneyCell("shortfall_amount", { negativeTone: true }),
      },
      {
        colId: "penalty",
        headerName: "Penalty",
        width: 112,
        ...num,
        valueGetter: (p) => p.data?.penalty_amount,
        comparator: moneyCompare,
        cellRenderer: moneyCell("penalty_amount", { negativeTone: true }),
      },
      {
        colId: "owed",
        headerName: "Total Owed",
        width: 128,
        ...num,
        valueGetter: (p) => p.data?.total_remediation_amount,
        comparator: moneyCompare,
        cellRenderer: moneyCell("total_remediation_amount", { emphasise: true, negativeTone: true }),
      },
      {
        colId: "capture",
        headerName: "PayPal Capture ID",
        width: 200,
        valueGetter: (p) => (p.data && !isTotals(p.data) ? p.data.paypal_capture_id : ""),
        cellRenderer: CaptureCell,
      },
      {
        colId: "status",
        headerName: "Compliance Status",
        width: 270,
        valueGetter: (p) => (p.data && !isTotals(p.data) ? (p.data.discrepancy_type ?? "") : ""),
        cellRenderer: StatusCell,
        getQuickFilterText: (p) =>
          p.data && !isTotals(p.data) && p.data.discrepancy_type ? `${COMPLIANCE_LABEL[p.data.discrepancy_type]} ${p.data.discrepancy_status}` : "",
      },
      {
        colId: "paidTeam",
        headerName: "Paid to team",
        width: 128,
        ...num,
        valueGetter: (p) => p.data?.paid_to_team,
        comparator: moneyCompare,
        cellRenderer: moneyCell("paid_to_team"),
      },
      { colId: "margin", headerName: "Margin", width: 124, ...num, valueGetter: (p) => p.data?.margin, comparator: moneyCompare, cellRenderer: MarginCell },
      {
        colId: "payroll",
        headerName: "Payroll",
        width: 150,
        valueGetter: (p) => (p.data && !isTotals(p.data) ? p.data.payroll_status : ""),
        cellRenderer: PayrollCell,
      },
      { colId: "action", headerName: "Action", width: 136, pinned: right, cellRenderer: ActionCell, sortable: false, resizable: false, suppressMovable: true },
    ],
    [left, right],
  );

  const defaultColDef = useMemo<ColDef<Row>>(() => ({ sortable: true, resizable: true, suppressHeaderMenuButton: true }), []);

  const recomputeTotals = useCallback(() => {
    const api = apiRef.current;
    if (!api) return;
    const shown: GridRow[] = [];
    api.forEachNodeAfterFilter((n) => {
      const d = n.data;
      if (d && !isTotals(d) && !d.__detailFor) shown.push(d);
    });
    const next = computeTotals(shown);
    // Only update when a number changed; a new pinned row re-renders the grid.
    setVisibleTotals((cur) => (cur && JSON.stringify(cur) === JSON.stringify(next) ? cur : next));
  }, []);

  const pinned = useMemo(() => [visibleTotals ?? computeTotals(joined)], [visibleTotals, joined]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by status">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              aria-pressed={status === f.id}
              onClick={() => setStatus(f.id)}
              className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset transition ${
                status === f.id ? "bg-ink text-white ring-ink" : "bg-white text-ink-soft ring-paper-line hover:bg-paper"
              }`}
            >
              {f.label}
              <span className={`num rounded-full px-1.5 text-[10px] ${status === f.id ? "bg-white/20" : "bg-slate-100 text-ink-muted"}`}>{counts[f.id]}</span>
            </button>
          ))}
        </div>
        <label className="relative w-full sm:w-72">
          <span className="sr-only">Search the ledger</span>
          <svg aria-hidden viewBox="0 0 20 20" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-muted" fill="none" stroke="currentColor" strokeWidth="1.8">
            <circle cx="9" cy="9" r="5.5" />
            <path d="m13.5 13.5 3 3" strokeLinecap="round" />
          </svg>
          <input
            value={quick}
            onChange={(e) => setQuick(e.target.value)}
            placeholder="Search client, milestone, capture ID…"
            className="h-9 w-full rounded-md border border-paper-line bg-white pl-9 pr-3 text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/20"
          />
        </label>
      </div>

      <div className="ag-theme-quartz fd-grid h-[560px] w-full" data-testid="ledger-grid">
        <AgGridReact<Row>
          rowData={rowData}
          columnDefs={colDefs}
          defaultColDef={defaultColDef}
          context={context}
          getRowId={(p: GetRowIdParams<Row>) => p.data.__key}
          rowHeight={58}
          headerHeight={42}
          getRowHeight={(p: RowHeightParams<Row>) => (p.data && !isTotals(p.data) && p.data.__detailFor ? 232 : undefined)}
          isFullWidthRow={(p: IsFullWidthRowParams<Row>) => Boolean(p.rowNode.data && !isTotals(p.rowNode.data) && p.rowNode.data.__detailFor)}
          fullWidthCellRenderer={DetailRow}
          pinnedBottomRowData={pinned}
          quickFilterText={quick}
          isExternalFilterPresent={() => status !== "all"}
          doesExternalFilterPass={(n) => {
            const d = n.data;
            if (!d || isTotals(d)) return true;
            return matchesStatus(d, status);
          }}
          getRowClass={(p: RowClassParams<Row>) => (p.data && !isTotals(p.data) && p.data.discrepancy_status === "PENDING_APPROVAL" ? "fd-needs-action" : undefined)}
          onGridReady={(e) => {
            apiRef.current = e.api;
            recomputeTotals();
          }}
          postSortRows={(p) => {
            // Keep each detail row directly under its milestone whatever the sort.
            const nodes = p.nodes;
            const details = nodes.filter((n) => n.data && !isTotals(n.data) && n.data.__detailFor);
            if (!details.length) return;
            const rest = nodes.filter((n) => !details.includes(n));
            for (const d of details) {
              const parent = (d.data as GridRow).__detailFor;
              const at = rest.findIndex((n) => n.data && !isTotals(n.data) && n.data.milestone_id === parent && !n.data.__detailFor);
              rest.splice(at + 1, 0, d);
            }
            nodes.splice(0, nodes.length, ...rest);
          }}
          onFilterChanged={recomputeTotals}
          onRowDataUpdated={recomputeTotals}
          loading={loading}
          suppressCellFocus
          animateRows
          overlayNoRowsTemplate="<span class='text-sm text-slate-500'>No milestones match. Activate a contract to start auditing.</span>"
        />
      </div>
    </div>
  );
}

function moneyCompare(a: string | null | undefined, b: string | null | undefined): number {
  return toCents(a) - toCents(b);
}

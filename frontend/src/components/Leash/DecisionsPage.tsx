import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact, type CustomCellRendererProps } from "ag-grid-react";
import type { ColDef, GetRowIdParams } from "ag-grid-community";
import { api } from "../../api/client";
import { Button } from "../common/Button";
import { Card, PageHeader } from "../common/Card";
import { Pill } from "../common/StatusBadge";
import { formatDateTime } from "../../lib/dates";
import { formatMoney } from "../../lib/money";
import type { DecisionLogEntry, DecisionSubjectType, GuardAction, LeashDecision } from "../../types/contracts";
import { errorText, flagLabel } from "./format";
import { selectCls } from "./styles";

const PAGE_SIZE = 50;

const ACTION_LABEL: Record<GuardAction, string> = {
  SEND_REMEDIATION_INVOICE: "Correction invoice",
  PAY_TEAM_MEMBER: "Pay team member",
  SEND_PAYROLL_BATCH: "Send payroll batch",
};

const SUBJECT_FILTERS: { value: "" | DecisionSubjectType; label: string }[] = [
  { value: "", label: "All" },
  { value: "discrepancy", label: "Correction invoices" },
  { value: "payroll_line", label: "Payroll lines" },
  { value: "payroll_run", label: "Payroll runs" },
];

const DECISION_LABEL: Record<LeashDecision, string> = { ALLOW: "Allow", ASK: "Ask", DENY: "Deny" };
const DECISION_TONE = { ALLOW: "good", ASK: "warn", DENY: "bad" } as const;

type Cell<V> = CustomCellRendererProps<DecisionLogEntry, V>;

function DecisionCell({ value }: Cell<LeashDecision>) {
  return value ? <Pill tone={DECISION_TONE[value]}>{DECISION_LABEL[value]}</Pill> : null;
}

function RuleChips({ value }: Cell<string[]>) {
  return (
    <span className="flex flex-wrap items-center gap-1">
      {(value ?? []).map((id) => (
        <Pill key={id} tone="neutral" title={id}>
          {flagLabel(id)}
        </Pill>
      ))}
    </span>
  );
}

/** Append-only log of every Leash decision, newest first. */
export default function DecisionsPage() {
  const [rows, setRows] = useState<DecisionLogEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [subject, setSubject] = useState<"" | DecisionSubjectType>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef(0);

  /** Load the first page (replace) or the next page (append). Errors stay inline. */
  const load = useCallback(async (filter: "" | DecisionSubjectType, after: string | null) => {
    const ticket = ++latest.current;
    setLoading(true);
    setError(null);
    try {
      const page = await api.decisions({
        limit: PAGE_SIZE,
        ...(filter ? { subject_type: filter } : {}),
        ...(after ? { cursor: after } : {}),
      });
      if (ticket !== latest.current) return;
      setRows((prev) => (after ? [...prev, ...page.items] : page.items));
      setCursor(page.next_cursor);
    } catch (e) {
      if (ticket !== latest.current) return;
      setError(errorText(e, "Could not load the decision log."));
    } finally {
      if (ticket === latest.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(subject, null);
  }, [load, subject]);

  const colDefs = useMemo<ColDef<DecisionLogEntry>[]>(
    () => [
      { colId: "time", headerName: "Time", field: "created_at", width: 160, valueFormatter: (p) => formatDateTime(p.value as string) },
      { colId: "action", headerName: "Action", field: "action_type", width: 180, valueFormatter: (p) => ACTION_LABEL[p.value as GuardAction] ?? String(p.value) },
      { colId: "subject", headerName: "Subject", field: "subject_id", width: 140 },
      { colId: "decision", headerName: "Decision", field: "decision", width: 110, cellRenderer: DecisionCell },
      { colId: "rules", headerName: "Rules", field: "rule_ids", flex: 1, minWidth: 250, sortable: false, cellRenderer: RuleChips },
      {
        colId: "amount",
        headerName: "Amount",
        field: "amount",
        width: 120,
        type: "rightAligned",
        valueFormatter: (p) => (p.value ? formatMoney(p.value as string) : ""),
      },
      { colId: "reason", headerName: "Reason", field: "reason", flex: 2, minWidth: 260, wrapText: true, autoHeight: true },
      { colId: "actor", headerName: "By", field: "actor", width: 90, valueFormatter: (p) => (p.value === "owner" ? "You" : "Leash") },
    ],
    [],
  );
  const defaultColDef = useMemo<ColDef<DecisionLogEntry>>(() => ({ sortable: true, resizable: true, suppressHeaderMenuButton: true }), []);

  return (
    <div>
      <PageHeader
        title="Decisions"
        description="Every time Leash allowed, held or blocked a money action, with the rule behind it. The log is never edited."
        actions={
          <label className="flex items-center gap-2 whitespace-nowrap text-[13px] text-ink-soft">
            Subject type
            <select
              className={`${selectCls} w-auto`}
              value={subject}
              onChange={(e) => setSubject(e.target.value as "" | DecisionSubjectType)}
            >
              {SUBJECT_FILTERS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
        }
      />
      <Card title="Decision log">
        {error && (
          <div role="alert" className="mb-4 flex flex-wrap items-center gap-3 border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
            <span>{error}</span>
            <Button size="sm" variant="secondary" onClick={() => void load(subject, null)}>
              Retry
            </Button>
          </div>
        )}
        <div role="region" aria-label="Leash decisions" className="ag-theme-quartz fd-grid w-full" data-testid="decisions-grid">
          <AgGridReact<DecisionLogEntry>
            rowData={rows}
            columnDefs={colDefs}
            defaultColDef={defaultColDef}
            getRowId={(p: GetRowIdParams<DecisionLogEntry>) => p.data.decision_id}
            domLayout="autoHeight"
            rowHeight={52}
            headerHeight={42}
            suppressColumnVirtualisation
            suppressCellFocus
            loading={loading && rows.length === 0}
            overlayNoRowsTemplate="<span style='color:#6b7f7c;font-size:14px'>No decisions yet.</span>"
          />
        </div>
        {cursor && (
          <div className="mt-4">
            <Button variant="secondary" onClick={() => void load(subject, cursor)} disabled={loading}>
              Load more
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}

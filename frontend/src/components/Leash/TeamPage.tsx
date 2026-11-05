import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { AgGridReact, type CustomCellRendererProps } from "ag-grid-react";
import type { ColDef, GetRowIdParams } from "ag-grid-community";
import { api, isApiError } from "../../api/client";
import { Button } from "../common/Button";
import { Card, PageHeader } from "../common/Card";
import { Modal } from "../common/Modal";
import { Pill } from "../common/StatusBadge";
import { formatDate } from "../../lib/dates";
import { formatMoney, isMoney, normaliseMoney, toCents } from "../../lib/money";
import type { TeamMemberOut } from "../../types/contracts";
import { maskEmail } from "./format";

const RATE_ERROR = "Enter the rate in dollars and cents, like 25.00.";
const input =
  "h-10 w-full rounded border border-paper-line bg-white px-3 text-[14px] outline-none placeholder:text-ink-muted focus:border-ink";

interface GridContext {
  edit: (m: TeamMemberOut) => void;
}
type CellProps = CustomCellRendererProps<TeamMemberOut, unknown, GridContext>;

function validRate(value: string): boolean {
  return isMoney(value) && /^\d+(\.\d{1,2})?$/.test(value.trim()) && toCents(value) > 0;
}

/** A server rejection that belongs to a field, shown in words. */
function serverMessage(e: unknown): string {
  if (isApiError(e)) return e.message;
  return "Something went wrong. Try again.";
}

function EditCell({ data, context }: CellProps) {
  if (!data) return null;
  return (
    <Button size="sm" variant="secondary" aria-label={`Edit ${data.name}`} onClick={() => context.edit(data)}>
      Edit
    </Button>
  );
}

/** The roster Leash pays from. Changing a payout email puts the next payment on hold (PR-01). */
export default function TeamPage() {
  const [members, setMembers] = useState<TeamMemberOut[]>([]);
  const [loading, setLoading] = useState(true);

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [rate, setRate] = useState("");
  const [adding, setAdding] = useState(false);
  const [rateError, setRateError] = useState<string | null>(null);
  const [addError, setAddError] = useState<string | null>(null);

  const [editing, setEditing] = useState<TeamMemberOut | null>(null);
  const [editEmail, setEditEmail] = useState("");
  const [editRate, setEditRate] = useState("");
  const [editRateError, setEditRateError] = useState<string | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [confirmEmail, setConfirmEmail] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .team()
      .then((rows) => live && setMembers(rows))
      .catch(() => undefined) // the global toast already told the owner
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, []);

  const openEdit = useCallback((m: TeamMemberOut) => {
    setEditing(m);
    setEditEmail(m.payout_email);
    setEditRate(m.hourly_rate);
    setEditRateError(null);
    setEditError(null);
    setConfirmEmail(false);
  }, []);

  const closeEdit = useCallback(() => setEditing(null), []);

  const submitAdd = async (e: FormEvent) => {
    e.preventDefault();
    setAddError(null);
    if (!validRate(rate)) {
      setRateError(RATE_ERROR);
      return;
    }
    setRateError(null);
    setAdding(true);
    try {
      const created = await api.addTeamMember({
        name: name.trim(),
        payout_email: email.trim(),
        hourly_rate: normaliseMoney(rate),
      });
      setMembers((rows) => [...rows, created]);
      setName("");
      setEmail("");
      setRate("");
    } catch (err) {
      setAddError(serverMessage(err));
    } finally {
      setAdding(false);
    }
  };

  const emailChanged = editing !== null && editEmail.trim().toLowerCase() !== editing.payout_email.toLowerCase();

  const save = async () => {
    if (!editing) return;
    setEditError(null);
    setSaving(true);
    try {
      const body: { payout_email?: string; hourly_rate?: string } = {};
      if (emailChanged) body.payout_email = editEmail.trim();
      if (toCents(editRate) !== toCents(editing.hourly_rate)) body.hourly_rate = normaliseMoney(editRate);
      const updated = await api.updateTeamMember(editing.member_id, body);
      setMembers((rows) => rows.map((m) => (m.member_id === updated.member_id ? updated : m)));
      setEditing(null);
    } catch (err) {
      setConfirmEmail(false);
      setEditError(serverMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const onSaveClick = (e: FormEvent) => {
    e.preventDefault();
    if (!editing) return;
    if (!validRate(editRate)) {
      setEditRateError(RATE_ERROR);
      return;
    }
    setEditRateError(null);
    if (emailChanged) setConfirmEmail(true);
    else void save();
  };

  const colDefs = useMemo<ColDef<TeamMemberOut>[]>(
    () => [
      { colId: "name", headerName: "Name", field: "name", flex: 1, minWidth: 140 },
      {
        colId: "email",
        headerName: "Payout email",
        minWidth: 220,
        flex: 1.4,
        valueGetter: (p) => (p.data ? maskEmail(p.data.payout_email) : ""),
        tooltipValueGetter: () => "Shown masked",
      },
      {
        colId: "rate",
        headerName: "Hourly rate",
        width: 130,
        type: "rightAligned",
        headerClass: "ag-right-aligned-header",
        cellClass: "num",
        valueGetter: (p) => formatMoney(p.data?.hourly_rate),
        comparator: (_a, _b, na, nb) => toCents(na.data?.hourly_rate) - toCents(nb.data?.hourly_rate),
      },
      {
        colId: "firstPaid",
        headerName: "First paid",
        width: 150,
        cellRenderer: ({ data }: CellProps) =>
          data?.first_paid_at ? <span className="num text-ink-soft">{formatDate(data.first_paid_at)}</span> : <span className="text-[13px] text-ink-muted">Not yet</span>,
        valueGetter: (p) => p.data?.first_paid_at ?? "",
      },
      {
        colId: "emailChanged",
        headerName: "Email changed",
        width: 170,
        cellRenderer: ({ data }: CellProps) =>
          data?.email_changed_at ? <span className="num text-warn">{formatDate(data.email_changed_at)}</span> : <span className="text-[13px] text-ink-muted">Never</span>,
        valueGetter: (p) => p.data?.email_changed_at ?? "",
      },
      {
        colId: "active",
        headerName: "Active",
        width: 110,
        cellRenderer: ({ data }: CellProps) => (data ? <Pill tone={data.active ? "good" : "neutral"}>{data.active ? "Active" : "Inactive"}</Pill> : null),
        valueGetter: (p) => (p.data?.active ? "Active" : "Inactive"),
      },
      { colId: "actions", headerName: "", width: 100, sortable: false, resizable: false, cellRenderer: EditCell },
    ],
    [],
  );

  const defaultColDef = useMemo<ColDef<TeamMemberOut>>(() => ({ sortable: true, resizable: true, suppressHeaderMenuButton: true }), []);
  const context = useMemo<GridContext>(() => ({ edit: openEdit }), [openEdit]);

  return (
    <div>
      <PageHeader
        title="Team"
        description="The people Leash pays. Payments go to the email on file; a changed email is held for your review."
      />

      <div className="flex flex-col gap-6">
        <Card title="Roster">
          <div role="region" aria-label="Team members" className="ag-theme-quartz fd-grid w-full" data-testid="team-grid">
            <AgGridReact<TeamMemberOut>
              rowData={members}
              columnDefs={colDefs}
              defaultColDef={defaultColDef}
              context={context}
              getRowId={(p: GetRowIdParams<TeamMemberOut>) => p.data.member_id}
              domLayout="autoHeight"
              rowHeight={52}
              headerHeight={42}
              suppressColumnVirtualisation
              suppressCellFocus
              loading={loading}
              overlayNoRowsTemplate="<span style='color:#6b7f7c;font-size:14px'>No team members yet. Add the first one below.</span>"
            />
          </div>
        </Card>

        <Card title="Add member">
          <form onSubmit={submitAdd} className="grid gap-4 sm:grid-cols-[1fr_1.4fr_9rem_auto] sm:items-start" noValidate>
            <label className="block">
              <span className="text-[13px] font-medium text-ink-soft">Name</span>
              <input className={`${input} mt-1`} value={name} onChange={(e) => setName(e.target.value)} required autoComplete="off" />
            </label>
            <label className="block">
              <span className="text-[13px] font-medium text-ink-soft">Payout email</span>
              <input
                className={`${input} mt-1`}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoComplete="off"
              />
            </label>
            <div>
              <label className="block">
                <span className="text-[13px] font-medium text-ink-soft">Hourly rate</span>
                <input
                  className={`${input} num mt-1`}
                  inputMode="decimal"
                  placeholder="25.00"
                  value={rate}
                  onChange={(e) => setRate(e.target.value)}
                  aria-invalid={rateError ? true : undefined}
                  aria-describedby={rateError ? "add-rate-error" : undefined}
                  required
                />
              </label>
              {rateError && (
                <span id="add-rate-error" role="alert" className="mt-1 block text-[13px] text-bad">
                  {rateError}
                </span>
              )}
            </div>
            <div className="sm:pt-[22px]">
              <Button type="submit" disabled={adding || !name.trim() || !email.trim() || !rate.trim()}>
                Add member
              </Button>
            </div>
          </form>
          {addError && (
            <p role="alert" className="mt-4 border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
              {addError}
            </p>
          )}
        </Card>
      </div>

      <Modal
        open={editing !== null}
        onClose={closeEdit}
        width="sm"
        title={confirmEmail ? "Confirm email change" : editing ? `Edit ${editing.name}` : "Edit member"}
        footer={
          confirmEmail ? (
            <>
              <Button variant="ghost" onClick={() => setConfirmEmail(false)} disabled={saving}>
                Back
              </Button>
              <Button onClick={() => void save()} disabled={saving}>
                Change email
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" onClick={closeEdit}>
                Cancel
              </Button>
              <Button type="submit" form="edit-member-form" disabled={saving}>
                Save changes
              </Button>
            </>
          )
        }
      >
        {editing && confirmEmail ? (
          <p className="text-[15px] leading-relaxed text-ink">
            {`Changing ${editing.name}'s payout email puts their next payment on hold until you confirm it (Leash check PR-01).`}
          </p>
        ) : (
          editing && (
            <form id="edit-member-form" onSubmit={onSaveClick} className="space-y-4" noValidate>
              <label className="block" htmlFor="edit-email">
                <span className="text-[13px] font-medium text-ink-soft">Payout email</span>
              </label>
              <input id="edit-email" className={input} type="email" value={editEmail} onChange={(e) => setEditEmail(e.target.value)} required />
              <label className="block" htmlFor="edit-rate">
                <span className="text-[13px] font-medium text-ink-soft">Hourly rate</span>
              </label>
              <input
                id="edit-rate"
                className={`${input} num`}
                inputMode="decimal"
                value={editRate}
                onChange={(e) => setEditRate(e.target.value)}
                aria-invalid={editRateError ? true : undefined}
                required
              />
              {editRateError && (
                <p role="alert" className="text-[13px] text-bad">
                  {editRateError}
                </p>
              )}
              {emailChanged && (
                <p className="text-[13px] text-warn">A changed payout email puts the next payment on hold until you confirm it.</p>
              )}
              {editError && (
                <p role="alert" className="border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
                  {editError}
                </p>
              )}
            </form>
          )
        )}
        {editing && confirmEmail && editError && (
          <p role="alert" className="mt-4 text-[14px] text-bad">
            {editError}
          </p>
        )}
      </Modal>
    </div>
  );
}

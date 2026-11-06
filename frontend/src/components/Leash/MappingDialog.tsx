import { useMemo, useState } from "react";
import { Button } from "../common/Button";
import { Modal } from "../common/Modal";
import type { ColumnMappingProposal, TimesheetField } from "../../types/contracts";
import { selectCls } from "./styles";
import { headerCollision, mappingProblem, TARGETS } from "./mapping";


interface Props {
  open: boolean;
  headers: string[];
  proposal: ColumnMappingProposal;
  rowCount: number;
  busy: boolean;
  /** An answer from the server (422, 409...) shown in the dialog. */
  error: string | null;
  onConfirm: (mapping: Record<string, TimesheetField>) => void;
  onDiscard: () => void;
  onClose: () => void;
}

/** Ask the owner which column is which. Nothing is stored until they confirm. */
export default function MappingDialog({ open, headers, proposal, rowCount, busy, error, onConfirm, onDiscard, onClose }: Props) {
  const [choice, setChoice] = useState<Record<string, TimesheetField | "">>(() =>
    Object.fromEntries(headers.map((h) => [h, proposal.mapping[h] ?? ""])),
  );
  // A duplicate target in the proposal itself is shown at once; "missing" waits until the owner tries to confirm.
  const [touched, setTouched] = useState(() => mappingProblem(choice)?.startsWith("Only one") ?? false);
  const problem = useMemo(() => mappingProblem(choice), [choice]);
  const collision = useMemo(() => headerCollision(headers), [headers]);

  const pick = (header: string, value: string) => {
    setChoice((c) => ({ ...c, [header]: value as TimesheetField | "" }));
  };

  const confirm = () => {
    setTouched(true);
    if (problem || collision) return;
    const mapping: Record<string, TimesheetField> = {};
    for (const [header, field] of Object.entries(choice)) if (field) mapping[header] = field;
    onConfirm(mapping);
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      width="md"
      title="Match the timesheet columns"
      subtitle={`${rowCount} rows. Check what Leash guessed, then confirm.`}
      footer={
        <>
          <Button variant="ghost" onClick={onDiscard} disabled={busy}>
            Discard upload
          </Button>
          <Button onClick={confirm} disabled={busy || collision !== null || (touched && problem !== null)}>
            Confirm mapping
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {headers.map((header, i) => {
          const conf = proposal.confidence[header];
          const id = `map-${i}`;
          return (
            <div key={header} className="grid grid-cols-[1fr_1fr] items-center gap-3">
              <label htmlFor={id} className="text-[14px] text-ink">
                <span className="font-medium">{header}</span>
                {conf !== undefined && proposal.mapping[header] && (
                  <span className="num ml-2 text-[12px] text-ink-muted">{Math.round(conf * 100)}% sure</span>
                )}
              </label>
              <select id={id} className={selectCls} value={choice[header] ?? ""} onChange={(e) => pick(header, e.target.value)} aria-describedby={problem && touched ? "map-problem" : undefined}>
                <option value="">(ignore)</option>
                {TARGETS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>
          );
        })}
        <p className="text-[12px] text-ink-muted">Required: member, work_date, hours, milestone_code. Rate is optional.</p>
        {collision && (
          <p id="map-collision" role="alert" className="text-[13px] text-bad">
            {collision}
          </p>
        )}
        {touched && problem && (
          <p id="map-problem" role="alert" className="text-[13px] text-bad">
            {problem}
          </p>
        )}
        {error && (
          <p role="alert" className="border-l-[3px] border-bad py-1 pl-3 text-[14px] text-bad">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}

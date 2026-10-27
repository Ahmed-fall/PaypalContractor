import type { ReactNode } from "react";
import type { PersistedContractExtraction, PersistedMilestoneSpec } from "../../types/contracts";
import { Pill } from "../common/StatusBadge";
import { emptyMilestone, type FieldErrors } from "./validation";
import { formatMoney } from "../../lib/money";

interface Props {
  value: PersistedContractExtraction;
  onChange: (next: PersistedContractExtraction) => void;
  errors: FieldErrors;
  readOnly: boolean;
  aiValues: PersistedContractExtraction | null; // what the AI returned, to flag edits
}

const inputCls =
  "w-full rounded-md border px-3 py-2 text-sm text-ink outline-none transition focus:border-brand focus:ring-2 focus:ring-brand/20 disabled:bg-paper disabled:text-ink-soft";

function Field({
  label,
  error,
  hint,
  edited,
  children,
  className = "",
}: {
  label: string;
  error?: string;
  hint?: ReactNode;
  edited?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`block ${className}`}>
      <span className="flex items-center gap-2 text-xs font-medium text-ink-soft">
        {label}
        {edited && <span className="rounded bg-slate-100 px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide text-ink-muted">Edited</span>}
      </span>
      <div className="mt-1">{children}</div>
      {error ? <span className="mt-1 block text-xs text-bad">{error}</span> : hint ? <span className="mt-1 block text-xs text-ink-muted">{hint}</span> : null}
    </label>
  );
}

export function ConfidenceBadge({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const tone = value >= 0.9 ? "good" : value >= 0.75 ? "warn" : "bad";
  return (
    <Pill tone={tone} title="How sure the AI is about this extraction. Leash auto-sends only at 90% or above (G-01).">
      AI confidence {pct}%
    </Pill>
  );
}

/** Right half of the split view: the extracted rules, editable before activation. */
export function RulesEditor({ value, onChange, errors, readOnly, aiValues }: Props) {
  const set = <K extends keyof PersistedContractExtraction>(k: K, v: PersistedContractExtraction[K]) => onChange({ ...value, [k]: v });
  const setMs = (i: number, patch: Partial<PersistedMilestoneSpec>) =>
    onChange({ ...value, milestones: value.milestones.map((m, j) => (j === i ? { ...m, ...patch } : m)) });
  const edited = (k: keyof PersistedContractExtraction) => Boolean(aiValues) && aiValues![k] !== value[k];
  const cls = (key: string) => `${inputCls} ${errors[key] ? "border-bad/60" : "border-paper-line"}`;

  return (
    <div className="space-y-6">
      <section className="grid gap-4 sm:grid-cols-2">
        <Field label="Contract title" error={errors.contract_title} edited={edited("contract_title")} className="sm:col-span-2">
          <input className={cls("contract_title")} value={value.contract_title} disabled={readOnly} onChange={(e) => set("contract_title", e.target.value)} />
        </Field>
        <Field label="Client" error={errors.client_name} edited={edited("client_name")}>
          <input className={cls("client_name")} value={value.client_name} disabled={readOnly} onChange={(e) => set("client_name", e.target.value)} />
        </Field>
        <Field label="Client PayPal email" error={errors.client_paypal_email} edited={edited("client_paypal_email")} hint="Correction invoices go here.">
          <input type="email" className={cls("client_paypal_email")} value={value.client_paypal_email} disabled={readOnly} onChange={(e) => set("client_paypal_email", e.target.value)} />
        </Field>
      </section>

      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-muted">Payment rules</h3>
        <div className="mt-3 grid gap-4 sm:grid-cols-4">
          <Field label="Currency" error={errors.currency} edited={edited("currency")}>
            <input className={`${cls("currency")} font-mono uppercase`} maxLength={3} value={value.currency} disabled={readOnly} onChange={(e) => set("currency", e.target.value.toUpperCase())} />
          </Field>
          <Field label="Late fee %" error={errors.late_fee_percentage} edited={edited("late_fee_percentage")}>
            <input inputMode="decimal" className={`${cls("late_fee_percentage")} font-mono`} value={value.late_fee_percentage} disabled={readOnly} onChange={(e) => set("late_fee_percentage", e.target.value)} />
          </Field>
          <Field label="Grace days" error={errors.grace_period_days} edited={edited("grace_period_days")}>
            <input
              type="number"
              min={0}
              className={`${cls("grace_period_days")} font-mono`}
              value={Number.isNaN(value.grace_period_days) ? "" : value.grace_period_days}
              disabled={readOnly}
              onChange={(e) => set("grace_period_days", e.target.value === "" ? Number.NaN : Number(e.target.value))}
            />
          </Field>
          <Field label="Partial payments" edited={edited("allow_partial_payments")}>
            <select
              className={cls("allow_partial_payments")}
              value={value.allow_partial_payments ? "yes" : "no"}
              disabled={readOnly}
              onChange={(e) => set("allow_partial_payments", e.target.value === "yes")}
            >
              <option value="no">Forbidden</option>
              <option value="yes">Permitted</option>
            </select>
          </Field>
          <Field label="Penalty clause (quoted)" edited={edited("penalty_clause_quote")} className="sm:col-span-4" hint="The exact text that justifies a late fee. Leash needs it quoted to auto-send.">
            <textarea rows={3} className={`${cls("penalty_clause_quote")} leading-relaxed`} value={value.penalty_clause_quote} disabled={readOnly} onChange={(e) => set("penalty_clause_quote", e.target.value)} />
          </Field>
        </div>
      </section>

      <section>
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-muted">Milestones</h3>
          {!readOnly && (
            <button
              type="button"
              className="text-xs font-medium text-brand hover:underline"
              onClick={() => onChange({ ...value, milestones: [...value.milestones, emptyMilestone(value.milestones.length)] })}
            >
              + Add milestone
            </button>
          )}
        </div>
        {errors.milestones && <p className="mt-2 text-xs text-bad">{errors.milestones}</p>}
        <div className="mt-3 space-y-3">
          {value.milestones.map((m, i) => (
            <div key={m.milestone_id || i} className="rounded-lg border border-paper-line bg-paper/40 p-4">
              <div className="grid gap-3 sm:grid-cols-[110px_1fr_140px_160px]">
                <Field label="Code" error={errors[`m${i}.milestone_code`]}>
                  <input className={`${cls(`m${i}.milestone_code`)} font-mono`} value={m.milestone_code} disabled={readOnly} onChange={(e) => setMs(i, { milestone_code: e.target.value })} />
                </Field>
                <Field label="Title" error={errors[`m${i}.title`]}>
                  <input className={cls(`m${i}.title`)} value={m.title} disabled={readOnly} onChange={(e) => setMs(i, { title: e.target.value })} />
                </Field>
                <Field
                  label={`Amount (${value.currency || "USD"})`}
                  error={errors[`m${i}.expected_amount`]}
                  hint={m.expected_amount && !errors[`m${i}.expected_amount`] ? formatMoney(m.expected_amount, value.currency || "USD") : undefined}
                >
                  <input inputMode="decimal" className={`${cls(`m${i}.expected_amount`)} text-right font-mono`} value={m.expected_amount} disabled={readOnly} onChange={(e) => setMs(i, { expected_amount: e.target.value })} />
                </Field>
                <Field label="Due date" error={errors[`m${i}.due_date`]}>
                  <input type="date" className={`${cls(`m${i}.due_date`)} font-mono`} value={m.due_date} disabled={readOnly} onChange={(e) => setMs(i, { due_date: e.target.value })} />
                </Field>
              </div>
              <Field label="Source clause" className="mt-3">
                <textarea rows={2} className={`${cls(`m${i}.source_clause_quote`)} leading-relaxed`} value={m.source_clause_quote} disabled={readOnly} onChange={(e) => setMs(i, { source_clause_quote: e.target.value })} />
              </Field>
              {!readOnly && value.milestones.length > 1 && (
                <div className="mt-2 text-right">
                  <button
                    type="button"
                    className="text-xs text-ink-muted hover:text-bad"
                    onClick={() => onChange({ ...value, milestones: value.milestones.filter((_, j) => j !== i) })}
                  >
                    Remove milestone
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

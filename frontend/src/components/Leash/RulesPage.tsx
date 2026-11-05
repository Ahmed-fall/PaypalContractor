import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, isApiError, request } from "../../api/client";
import { Button } from "../common/Button";
import { Card, PageHeader } from "../common/Card";
import { Pill } from "../common/StatusBadge";
import { formatDateTime } from "../../lib/dates";
import type { ActiveLeashPolicy, LeashPolicy, PolicyCompileResponse } from "../../types/contracts";
import { savePolicyDraft } from "./api";
import {
  describeRule,
  formFromPolicy,
  policyFromForm,
  validateRulesForm,
  type RulesForm,
  type RulesFormErrors,
} from "./rules";

const COMPILER_UNAVAILABLE = "The AI rule writer isn't available yet. Use the form below.";
const input =
  "h-10 w-full rounded border border-paper-line bg-white px-3 text-[14px] outline-none placeholder:text-ink-muted focus:border-ink";

/** Server 422 field names -> the form field they belong to. */
const SERVER_FIELD: Record<string, keyof RulesForm> = {
  email_change_window_days: "emailWindowDays",
  payroll_margin_limit_pct: "marginLimitPct",
};

function serverFieldErrors(e: unknown): { fields: RulesFormErrors; general: string | null } {
  if (!isApiError(e)) return { fields: {}, general: "Something went wrong. Try again." };
  const fields: RulesFormErrors = {};
  const list = (e.details as { fields?: unknown } | null)?.fields;
  let general: string | null = null;
  if (Array.isArray(list)) {
    for (const item of list as Array<{ field?: string; msg?: string }>) {
      const target = item.field ? SERVER_FIELD[item.field] : undefined;
      if (target) fields[target] = item.msg ?? e.message;
      else general = item.msg ?? e.message;
    }
  }
  if (!Object.keys(fields).length && !general) general = e.message;
  return { fields, general };
}

function RuleCards({ policy }: { policy: LeashPolicy }) {
  return (
    <ul className="flex flex-col gap-2" aria-label="Rule cards">
      {policy.rules.map((rule) => (
        <li key={rule.id} className="flex items-start gap-3 rounded border border-paper-rule bg-paper px-4 py-3 text-[14px] text-ink">
          <span className="num mt-0.5 shrink-0 text-[12px] font-semibold text-ink-muted">{rule.id}</span>
          <span>{describeRule(rule)}</span>
        </li>
      ))}
      <li className="rounded border border-paper-rule bg-paper px-4 py-3 text-[14px] text-ink">
        Payroll: a changed payout email (last {policy.email_change_window_days} days) waits for you.
      </li>
      <li className="rounded border border-paper-rule bg-paper px-4 py-3 text-[14px] text-ink">
        Payroll: a run above {policy.payroll_margin_limit_pct}% of the money received waits for you.
      </li>
    </ul>
  );
}

function Field({ id, label, hint, error, children }: { id: string; label: string; hint?: string; error?: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-[13px] font-medium text-ink">
        {label}
      </label>
      {children}
      {hint && !error && <p className="mt-1 text-[12px] text-ink-muted">{hint}</p>}
      {error && (
        <p role="alert" className="mt-1 text-[12px] text-bad">
          {error}
        </p>
      )}
    </div>
  );
}

/** What Leash is allowed to do on its own. Nothing changes until a draft is activated. */
export default function RulesPage() {
  const [active, setActive] = useState<ActiveLeashPolicy | null>(null);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState<RulesForm | null>(null);
  const [errors, setErrors] = useState<RulesFormErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);

  const [text, setText] = useState("");
  const [compiling, setCompiling] = useState(false);
  const [compileMsg, setCompileMsg] = useState<string | null>(null);

  const [draft, setDraft] = useState<PolicyCompileResponse | null>(null);
  const [activating, setActivating] = useState(false);
  const [activateError, setActivateError] = useState<string | null>(null);
  const [activatedVersion, setActivatedVersion] = useState<number | null>(null);

  const load = useCallback(async () => {
    const policy = await api.activePolicy();
    setActive(policy);
    return policy;
  }, []);

  useEffect(() => {
    let live = true;
    load()
      .then((policy) => live && setForm((f) => f ?? formFromPolicy(policy)))
      .catch(() => undefined) // the global toast already told the owner
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [load]);

  const setField = <K extends keyof RulesForm>(key: K, value: RulesForm[K]) => {
    setForm((f) => (f ? { ...f, [key]: value } : f));
    setErrors((e) => ({ ...e, [key]: undefined }));
  };

  const onPreview = async (e: FormEvent) => {
    e.preventDefault();
    if (!form || !active) return;
    const found = validateRulesForm(form);
    setFormError(null);
    setErrors(found);
    if (Object.keys(found).length) return;
    setPreviewing(true);
    setActivatedVersion(null);
    try {
      setDraft(await savePolicyDraft(policyFromForm(form, active)));
      setActivateError(null);
    } catch (err) {
      const { fields, general } = serverFieldErrors(err);
      setErrors(fields);
      setFormError(general);
    } finally {
      setPreviewing(false);
    }
  };

  const onCompile = async () => {
    const body = text.trim();
    if (!body) return;
    setCompiling(true);
    setCompileMsg(null);
    setActivatedVersion(null);
    try {
      const res = await request<PolicyCompileResponse>("POST", "/leash/policy/compile", { body: { text: body }, quiet: true });
      setDraft(res);
      setActivateError(null);
    } catch (err) {
      if (isApiError(err) && (err.status === 501 || err.code === "COMPILER_UNAVAILABLE")) setCompileMsg(COMPILER_UNAVAILABLE);
      else setCompileMsg(isApiError(err) ? err.message : "Something went wrong. Try again.");
    } finally {
      setCompiling(false);
    }
  };

  const onActivate = async () => {
    if (!draft || activating) return;
    setActivating(true);
    setActivateError(null);
    try {
      const res = await api.activatePolicy({ version: draft.draft_version });
      setActivatedVersion(res.version);
      setDraft(null);
      const policy = await load();
      setForm(formFromPolicy(policy));
    } catch (err) {
      setActivateError(isApiError(err) ? err.message : "Something went wrong. Try again.");
    } finally {
      setActivating(false);
    }
  };

  return (
    <div>
      <PageHeader
        title="Rules"
        description="What Leash may do on its own and what it must ask you first. Nothing changes until you activate a new version."
      />

      <div className="flex flex-col gap-6">
        <Card
          title="Active rules"
          action={
            active && (
              <span className="flex items-center gap-2 text-[13px] text-ink-muted">
                <Pill tone="good">Version {active.version}</Pill>
                <span>Active since {formatDateTime(active.activated_at)}</span>
              </span>
            )
          }
        >
          {loading && <p className="text-[14px] text-ink-muted">Loading the active rules...</p>}
          {active && <RuleCards policy={active} />}
          {activatedVersion !== null && (
            <p role="status" className="mt-3 text-[14px] text-good">
              Version {activatedVersion} is now active.
            </p>
          )}
        </Card>

        <Card title="Change rules">
          <label htmlFor="rules-text" className="mb-1 block text-[13px] font-medium text-ink">
            Describe the rules in your own words (optional)
          </label>
          <textarea
            id="rules-text"
            value={text}
            maxLength={4000}
            onChange={(e) => setText(e.target.value)}
            rows={3}
            placeholder="Send small correction invoices on my own, but ask me about anything over $500."
            className="w-full rounded border border-paper-line bg-white px-3 py-2 text-[14px] outline-none placeholder:text-ink-muted focus:border-ink"
          />
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button variant="secondary" onClick={onCompile} disabled={compiling || !text.trim()}>
              {compiling ? "Compiling..." : "Compile with AI"}
            </Button>
            {compileMsg && (
              <p role="alert" className="text-[13px] text-warn">
                {compileMsg}
              </p>
            )}
          </div>

          <hr className="my-5 border-paper-rule" />

          <h3 className="mb-3 text-[14px] font-semibold text-ink">Plain form</h3>
          {form && (
            <form onSubmit={onPreview} noValidate className="grid gap-4 sm:grid-cols-2" aria-label="Rules form">
              <Field
                id="rf-limit"
                label="Auto-send limit (USD)"
                hint="Correction invoices up to this amount go out on their own. Larger ones wait for you."
                error={errors.autoSendLimit}
              >
                <input id="rf-limit" className={`${input} num`} inputMode="decimal" value={form.autoSendLimit} onChange={(e) => setField("autoSendLimit", e.target.value)} />
              </Field>
              <Field id="rf-confidence" label="Minimum confidence" hint="How sure Leash must be of the contract reading, 0 to 1." error={errors.minConfidence}>
                <input id="rf-confidence" className={`${input} num`} inputMode="decimal" value={form.minConfidence} onChange={(e) => setField("minConfidence", e.target.value)} />
              </Field>
              <Field id="rf-repeat" label="Repeat-invoice window (days)" hint="A second invoice to the same client inside this window waits for you. 0 turns it off." error={errors.repeatWindowDays}>
                <input id="rf-repeat" className={`${input} num`} inputMode="numeric" value={form.repeatWindowDays} onChange={(e) => setField("repeatWindowDays", e.target.value)} />
              </Field>
              <Field id="rf-email" label="Email-change hold (days)" hint="A payout email changed this recently puts the next payment on hold." error={errors.emailWindowDays}>
                <input id="rf-email" className={`${input} num`} inputMode="numeric" value={form.emailWindowDays} onChange={(e) => setField("emailWindowDays", e.target.value)} />
              </Field>
              <Field id="rf-margin" label="Payroll cash limit (%)" hint="A run that spends more than this share of the money received waits for you." error={errors.marginLimitPct}>
                <input id="rf-margin" className={`${input} num`} inputMode="decimal" value={form.marginLimitPct} onChange={(e) => setField("marginLimitPct", e.target.value)} />
              </Field>
              <div className="flex items-center gap-2 self-center">
                <input
                  id="rf-never"
                  type="checkbox"
                  checked={form.neverAutoContact}
                  onChange={(e) => setField("neverAutoContact", e.target.checked)}
                  className="h-4 w-4"
                />
                <label htmlFor="rf-never" className="text-[14px] text-ink">
                  Always ask me first for clients tagged never_auto_contact
                </label>
              </div>
              <div className="sm:col-span-2">
                {formError && (
                  <p role="alert" className="mb-2 text-[13px] text-bad">
                    {formError}
                  </p>
                )}
                <Button type="submit" disabled={previewing}>
                  {previewing ? "Saving draft..." : "Preview"}
                </Button>
                <p className="mt-2 text-[12px] text-ink-muted">The invoice integrity check (G-05) is built in and always on.</p>
              </div>
            </form>
          )}
        </Card>

        {draft && (
          <Card
            title="Draft rules"
            action={
              <span className="flex items-center gap-2 text-[13px] text-ink-muted">
                <Pill tone="warn">DRAFT</Pill>
                <span>Version {draft.draft_version}</span>
              </span>
            }
          >
            <ul className="flex flex-col gap-2" aria-label="Draft cards">
              {draft.cards.map((card) => (
                <li key={card} className="rounded border border-paper-rule bg-paper px-4 py-3 text-[14px] text-ink">
                  {card}
                </li>
              ))}
            </ul>
            {draft.test_cases.length > 0 && (
              <ul className="mt-3 list-disc pl-5 text-[13px] text-ink-soft">
                {draft.test_cases.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            )}
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <Button onClick={onActivate} disabled={activating}>
                Activate version {draft.draft_version}
              </Button>
              <span className="text-[13px] text-ink-muted">Until you activate, Leash keeps using version {active?.version}.</span>
            </div>
            {activateError && (
              <p role="alert" className="mt-2 text-[13px] text-bad">
                {activateError}
              </p>
            )}
          </Card>
        )}
      </div>
    </div>
  );
}

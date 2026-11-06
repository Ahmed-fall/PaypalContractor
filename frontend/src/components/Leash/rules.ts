/**
 * Pure helpers for the Rules screen: plain-language rule cards and the plain form
 * (Fallback E) that builds a policy without the AI rule writer.
 *
 * The `when` keys are the guard's vocabulary (backend `leash/policy.py` default policy and
 * `api/leash.py::_condition_text`): amount_lte, amount_gt, confidence_gte,
 * clause_quote_present, client_tag, invoiced_same_client_within_days,
 * line_items_sum_mismatch. Amount thresholds are 2-dp money strings on the wire.
 */
import { formatMoney, isMoney, normaliseMoney } from "../../lib/money";
import type { GuardAction, LeashDecision, LeashPolicy, LeashRule, RuleConditionValue } from "../../types/contracts";

/** Ids the server refuses from a client: the built-in G-05 plus the guard's own result ids. */
export const RESERVED_RULE_IDS: readonly string[] = ["G-05", "DEFAULT", "PAYROLL-CHECKS"];
export const MAX_WINDOW_DAYS = 365;
export const NEVER_AUTO_CONTACT = "never_auto_contact";

const INVOICE: GuardAction = "SEND_REMEDIATION_INVOICE";
const FORM_RULE_IDS: readonly string[] = ["G-01", "G-02", "G-03", "G-04"];
const DEFAULT_LIMIT = "250.00";
const DEFAULT_CONFIDENCE = 0.9;

export interface RulesForm {
  autoSendLimit: string; // 2-dp money string; G-01 amount_lte and G-02 amount_gt
  minConfidence: string; // 0..1; G-01 confidence_gte
  neverAutoContact: boolean; // G-03
  repeatWindowDays: string; // days; G-04 (0 turns the rule off)
  marginLimitPct: string; // payroll_margin_limit_pct, above 0 up to 100
  emailWindowDays: string; // email_change_window_days, 0..365
}

export type RulesFormErrors = Partial<Record<keyof RulesForm, string>>;

const OUTCOME_PHRASE: Record<LeashDecision, string> = {
  ALLOW: "Leash goes ahead on its own",
  ASK: "Leash waits for you",
  DENY: "Leash blocks it",
};

function money(value: RuleConditionValue): string {
  return formatMoney(String(value));
}

function confidence(value: RuleConditionValue): string {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : String(value);
}

function conditionText(key: string, value: RuleConditionValue): string {
  switch (key) {
    case "amount_lte":
      return `the amount is ${money(value)} or less`;
    case "amount_gt":
      return `the amount is above ${money(value)}`;
    case "confidence_gte":
      return `the contract reading is at least ${confidence(value)} confident`;
    case "clause_quote_present":
      return value ? "the breached clause is quoted" : "the breached clause is not quoted";
    case "client_tag":
      return `the client is tagged ${String(value)}`;
    case "invoiced_same_client_within_days":
      return `the same client was invoiced in the last ${String(value)} days`;
    case "line_items_sum_mismatch":
      return "the line items don't add up";
    default:
      return `${key.replace(/_/g, " ")} is ${String(value)}`;
  }
}

const AMOUNT_SUBJECT: Record<GuardAction, string> = {
  SEND_REMEDIATION_INVOICE: "Correction invoices",
  PAY_TEAM_MEMBER: "Payments",
  SEND_PAYROLL_BATCH: "Payroll runs",
};

/** One plain sentence for a rule. Known `when` shapes read naturally; anything else still names the outcome. */
export function describeRule(rule: LeashRule): string {
  const when = rule.when;
  const keys = Object.keys(when).sort();
  const only = (allowed: string[]) => keys.every((k) => allowed.includes(k));
  const invoice = rule.applies_to === INVOICE;
  const noun = AMOUNT_SUBJECT[rule.applies_to] ?? "Actions";

  if (keys.includes("amount_lte") && only(["amount_lte", "confidence_gte", "clause_quote_present"])) {
    const extras: string[] = [];
    if (when.clause_quote_present === true) extras.push("the clause is quoted");
    if (when.clause_quote_present === false) extras.push("the clause is not quoted");
    if (when.confidence_gte !== undefined) {
      extras.push(`the contract is clear (confidence ≥ ${confidence(when.confidence_gte)})`);
    }
    const tail = extras.length ? ` when ${extras.join(" and ")}` : "";
    const base = `${noun} of ${money(when.amount_lte)} or less`;
    if (rule.outcome === "ALLOW") return `${base} go out on their own${tail}.`;
    if (rule.outcome === "ASK") return `${base} wait for you${tail}.`;
    return `${base} are blocked${tail}.`;
  }
  if (keys.length === 1 && keys[0] === "amount_gt") {
    const base = `${noun} above ${money(when.amount_gt)}`;
    if (rule.outcome === "ALLOW") return `${base} go out on their own.`;
    return `${base} ${rule.outcome === "ASK" ? "wait for you" : "are blocked"}.`;
  }
  if (invoice && keys.length === 1 && keys[0] === "client_tag") {
    const base = `Clients tagged ${String(when.client_tag)}`;
    if (rule.outcome === "ALLOW") return `${base} always go out on their own.`;
    return `${base} always ${rule.outcome === "ASK" ? "wait for you" : "are blocked"}.`;
  }
  if (invoice && keys.length === 1 && keys[0] === "invoiced_same_client_within_days") {
    const base = `A second invoice to the same client within ${String(when.invoiced_same_client_within_days)} days`;
    if (rule.outcome === "ALLOW") return `${base} goes out on its own.`;
    return `${base} ${rule.outcome === "ASK" ? "waits for you" : "is blocked"}.`;
  }
  if (invoice && keys.length === 1 && keys[0] === "line_items_sum_mismatch" && when.line_items_sum_mismatch === true && rule.outcome === "DENY") {
    return "Invoices whose line items don't add up are blocked (built in, can't be removed).";
  }
  const conditions = keys.map((k) => conditionText(k, when[k])).join(" and ");
  const prefix = conditions ? `When ${conditions}` : "Always";
  return `${prefix}, ${OUTCOME_PHRASE[rule.outcome]} (${rule.id}).`;
}

function findRule(policy: LeashPolicy, id: string): LeashRule | undefined {
  return policy.rules.find((r) => r.id === id);
}

function limitOf(policy: LeashPolicy): string {
  const raw = findRule(policy, "G-01")?.when.amount_lte ?? findRule(policy, "G-02")?.when.amount_gt;
  return raw !== undefined && isMoney(String(raw)) ? normaliseMoney(String(raw)) : DEFAULT_LIMIT;
}

/** Read the plain-form fields out of a policy. G-05 and other rules are not form fields. */
export function formFromPolicy(policy: LeashPolicy): RulesForm {
  const confidence = findRule(policy, "G-01")?.when.confidence_gte;
  const repeat = findRule(policy, "G-04")?.when.invoiced_same_client_within_days;
  return {
    autoSendLimit: limitOf(policy),
    minConfidence: (confidence !== undefined ? Number(confidence) : DEFAULT_CONFIDENCE).toFixed(2),
    neverAutoContact: findRule(policy, "G-03") !== undefined,
    repeatWindowDays: repeat !== undefined ? String(repeat) : "0",
    marginLimitPct: String(policy.payroll_margin_limit_pct),
    emailWindowDays: String(policy.email_change_window_days),
  };
}

const wholeDays = (v: string) => /^\d+$/.test(v.trim());

/** Client-side checks that mirror what the server refuses, so nothing it would 422 is sent. */
export function validateRulesForm(form: RulesForm): RulesFormErrors {
  const errors: RulesFormErrors = {};
  if (!isMoney(form.autoSendLimit)) errors.autoSendLimit = "Enter the limit in dollars and cents, like 250.00.";
  if (!/^(0(\.\d+)?|1(\.0+)?)$/.test(form.minConfidence.trim())) {
    errors.minConfidence = "Enter a number from 0 to 1, like 0.90.";
  }
  if (!wholeDays(form.repeatWindowDays) || Number(form.repeatWindowDays) > MAX_WINDOW_DAYS) {
    errors.repeatWindowDays = `Enter whole days from 0 to ${MAX_WINDOW_DAYS}. Use 0 to turn this off.`;
  }
  if (!wholeDays(form.emailWindowDays) || Number(form.emailWindowDays) > MAX_WINDOW_DAYS) {
    errors.emailWindowDays = `Enter whole days from 0 to ${MAX_WINDOW_DAYS}.`;
  }
  const pct = form.marginLimitPct.trim();
  if (!/^\d{1,3}(\.\d{1,2})?$/.test(pct) || Number(pct) <= 0 || Number(pct) > 100) {
    errors.marginLimitPct = "Enter a percentage above 0 and up to 100, like 80.";
  }
  return errors;
}

/**
 * Build the policy to save from the form. G-01..G-04 come from the form; any other rule in
 * `base` is kept as is. Reserved ids (G-05, DEFAULT, PAYROLL-CHECKS) are never sent: the
 * server appends the built-in G-05 itself and refuses reserved ids with a 422.
 */
export function policyFromForm(form: RulesForm, base: LeashPolicy): LeashPolicy {
  const limit = normaliseMoney(form.autoSendLimit);
  const previous = (id: string) => findRule(base, id);
  const rules: LeashRule[] = [
    {
      id: "G-01",
      applies_to: INVOICE,
      when: {
        ...previous("G-01")?.when,
        amount_lte: limit,
        confidence_gte: Number(form.minConfidence),
        clause_quote_present: true,
      },
      outcome: "ALLOW",
      source_text: previous("G-01")?.source_text ?? "Send correction invoices on my own if they are small and the contract is clear.",
    },
    {
      id: "G-02",
      applies_to: INVOICE,
      when: { ...previous("G-02")?.when, amount_gt: limit },
      outcome: "ASK",
      source_text: previous("G-02")?.source_text ?? "Ask me before sending larger correction invoices.",
    },
  ];
  if (form.neverAutoContact) {
    rules.push({
      id: "G-03",
      applies_to: INVOICE,
      when: { ...previous("G-03")?.when, client_tag: NEVER_AUTO_CONTACT },
      outcome: "ASK",
      source_text: previous("G-03")?.source_text ?? "Always ask me first for clients tagged never_auto_contact",
    });
  }
  const days = Number(form.repeatWindowDays);
  if (days > 0) {
    rules.push({
      id: "G-04",
      applies_to: INVOICE,
      when: { ...previous("G-04")?.when, invoiced_same_client_within_days: days },
      outcome: "ASK",
      source_text: previous("G-04")?.source_text ?? "Ask me if we already invoiced this client recently.",
    });
  }
  const others = base.rules.filter((r) => !FORM_RULE_IDS.includes(r.id) && !RESERVED_RULE_IDS.includes(r.id));
  // `version` is only a placeholder: the server assigns the real draft version (latest + 1) and ignores this one.
  return {
    version: base.version,
    rules: [...rules, ...others],
    payroll_margin_limit_pct: form.marginLimitPct.trim(),
    email_change_window_days: Number(form.emailWindowDays),
  };
}

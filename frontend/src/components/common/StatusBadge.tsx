import type { ReactNode } from "react";
import type { DiscrepancyStatus, DiscrepancyType, LeashDecision, MarginPayrollStatus, RemediationSentBy } from "../../types/contracts";

type Tone = "good" | "warn" | "bad" | "brand" | "neutral";

const toneClass: Record<Tone, string> = {
  good: "border-good/35 text-good",
  warn: "border-warn/35 text-warn",
  bad: "border-bad/40 text-bad",
  brand: "border-ink/35 text-ink",
  neutral: "border-paper-line text-ink-muted",
};

/** A stamped tag: outline and coloured text, no fill. Reads like a mark on a working paper. */
export function Pill({ tone, children, title, solid = false }: { tone: Tone; children: ReactNode; title?: string; solid?: boolean }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center whitespace-nowrap rounded-[3px] border px-1.5 py-px text-[12px] font-medium leading-[18px] ${
        solid ? "border-transparent bg-bad text-white" : toneClass[tone]
      }`}
    >
      {children}
    </span>
  );
}

export const COMPLIANCE_LABEL: Record<DiscrepancyType, string> = {
  COMPLIANT: "Paid in full",
  UNDERPAID: "Underpaid",
  LATE_PAYMENT: "Paid late",
  UNDERPAID_AND_LATE: "Underpaid and late",
  CURRENCY_MISMATCH: "Wrong currency",
};

const COMPLIANCE_TONE: Record<DiscrepancyType, Tone> = {
  COMPLIANT: "good",
  UNDERPAID: "bad",
  LATE_PAYMENT: "warn",
  UNDERPAID_AND_LATE: "bad",
  CURRENCY_MISMATCH: "warn",
};

/** Audit reason (immutable). null means no capture yet. */
export function ComplianceBadge({ type }: { type: DiscrepancyType | null }) {
  if (!type) return <Pill tone="neutral">Not paid yet</Pill>;
  return <Pill tone={COMPLIANCE_TONE[type]}>{COMPLIANCE_LABEL[type]}</Pill>;
}

/** Remediation lifecycle (Figure 8), shown as plain secondary text beside the reason. */
export function LifecycleBadge({ status, sentBy, decision }: { status: DiscrepancyStatus; sentBy: RemediationSentBy | null; decision: LeashDecision | null }) {
  const cls = "whitespace-nowrap text-[12px]";
  switch (status) {
    case "PENDING_APPROVAL":
      return (
        <span className={`${cls} font-medium text-bad`} title={decision === "ASK" ? "Leash asked for your approval" : undefined}>
          Needs your approval
        </span>
      );
    case "BLOCKED":
      return <span className={`${cls} font-medium text-bad`}>Blocked by Leash</span>;
    case "REMEDIATED":
      return <span className={`${cls} text-ink-muted`}>{sentBy === "leash-auto" ? "Invoiced by Leash" : "Invoiced by you"}</span>;
    default:
      return null;
  }
}

export function DecisionBadge({ decision }: { decision: LeashDecision }) {
  const label = decision === "ALLOW" ? "Allowed" : decision === "ASK" ? "Asks you" : "Denied";
  const tone: Tone = decision === "ALLOW" ? "good" : decision === "ASK" ? "brand" : "bad";
  return <Pill tone={tone}>Leash: {label}</Pill>;
}

const PAYROLL_LABEL: Record<MarginPayrollStatus, string> = {
  NONE: "No run",
  PREPARED: "Prepared",
  CHECKED: "Checked",
  AWAITING_APPROVAL: "Awaiting approval",
  SENT: "Sending",
  PAID: "Paid",
  PARTIAL: "Partly paid",
  FAILED: "Failed",
};

export function PayrollBadge({ status }: { status: MarginPayrollStatus }) {
  const c =
    status === "PAID" ? "text-good" : status === "FAILED" ? "text-bad" : status === "NONE" ? "text-ink-muted" : status === "PARTIAL" || status === "SENT" ? "text-warn" : "text-ink";
  return <span className={`whitespace-nowrap text-[13px] ${c}`}>{PAYROLL_LABEL[status]}</span>;
}

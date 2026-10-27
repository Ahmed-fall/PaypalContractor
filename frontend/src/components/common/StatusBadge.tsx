import type { DiscrepancyStatus, DiscrepancyType, LeashDecision, MarginPayrollStatus, RemediationSentBy } from "../../types/contracts";

type Tone = "good" | "warn" | "bad" | "brand" | "neutral";

const toneClass: Record<Tone, string> = {
  good: "bg-good-soft text-good ring-good/20",
  warn: "bg-warn-soft text-warn ring-warn/20",
  bad: "bg-bad-soft text-bad ring-bad/20",
  brand: "bg-brand-soft text-brand-dark ring-brand/20",
  neutral: "bg-slate-100 text-ink-soft ring-slate-300/40",
};

export function Pill({ tone, children, title }: { tone: Tone; children: React.ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${toneClass[tone]}`}
    >
      <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      {children}
    </span>
  );
}

export const COMPLIANCE_LABEL: Record<DiscrepancyType, string> = {
  COMPLIANT: "Compliant",
  UNDERPAID: "Underpaid",
  LATE_PAYMENT: "Late",
  UNDERPAID_AND_LATE: "Underpaid + late",
  CURRENCY_MISMATCH: "Currency mismatch",
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
  if (!type) return <Pill tone="neutral">Awaiting payment</Pill>;
  return <Pill tone={COMPLIANCE_TONE[type]}>{COMPLIANCE_LABEL[type]}</Pill>;
}

/** Remediation lifecycle (Figure 8), kept separate from the reason. */
export function LifecycleBadge({
  status,
  sentBy,
  decision,
}: {
  status: DiscrepancyStatus;
  sentBy: RemediationSentBy | null;
  decision: LeashDecision | null;
}) {
  switch (status) {
    case "PENDING_APPROVAL":
      return <Pill tone="brand" title={decision === "ASK" ? "Leash asked for your approval" : undefined}>Needs approval</Pill>;
    case "BLOCKED":
      return <Pill tone="bad">Blocked by Leash</Pill>;
    case "REMEDIATED":
      return sentBy === "leash-auto" ? <Pill tone="good">Auto-sent by Leash</Pill> : <Pill tone="good">Invoice sent</Pill>;
    default:
      return null;
  }
}

export function DecisionBadge({ decision }: { decision: LeashDecision }) {
  const tone: Tone = decision === "ALLOW" ? "good" : decision === "ASK" ? "brand" : "bad";
  return <Pill tone={tone}>Leash: {decision}</Pill>;
}

const PAYROLL_TONE: Record<MarginPayrollStatus, Tone> = {
  NONE: "neutral",
  PREPARED: "neutral",
  CHECKED: "brand",
  AWAITING_APPROVAL: "brand",
  SENT: "warn",
  PAID: "good",
  PARTIAL: "warn",
  FAILED: "bad",
};

export function PayrollBadge({ status }: { status: MarginPayrollStatus }) {
  const label = status === "NONE" ? "No run" : status.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
  return <Pill tone={PAYROLL_TONE[status]}>{label}</Pill>;
}

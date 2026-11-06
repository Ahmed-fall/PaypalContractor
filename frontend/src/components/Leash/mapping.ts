import type { TimesheetField } from "../../types/contracts";

/** Targets the backend accepts (TimesheetField in contracts.ts). `rate` is optional; the other four are required. */
export const TARGETS: Array<{ value: TimesheetField; label: string; required: boolean }> = [
  { value: "member", label: "Member", required: true },
  { value: "work_date", label: "Work date", required: true },
  { value: "hours", label: "Hours", required: true },
  { value: "milestone_code", label: "Milestone code", required: true },
  { value: "rate", label: "Rate", required: false },
];

/** Pure check, exported for the tests: a message when the choice can't be sent, else null. */
export function mappingProblem(choice: Record<string, TimesheetField | "">): string | null {
  const picked = Object.values(choice).filter((v): v is TimesheetField => v !== "");
  const seen = new Set<TimesheetField>();
  for (const field of picked) {
    if (seen.has(field)) {
      const label = TARGETS.find((t) => t.value === field)?.label ?? field;
      return `Only one column can be ${label}.`;
    }
    seen.add(field);
  }
  const missing = TARGETS.filter((t) => t.required && !seen.has(t.value)).map((t) => t.label);
  if (missing.length) return `Choose a column for: ${missing.join(", ")}.`;
  return null;
}


/** Headers the server treats as one column (it compares them trimmed and lowercased). */
export function headerCollision(headers: string[]): string | null {
  const seen = new Map<string, string>();
  for (const h of headers) {
    const key = h.trim().toLowerCase();
    const first = seen.get(key);
    if (first !== undefined) return `Two columns are both called "${key}". Rename one in the file and upload it again.`;
    seen.set(key, h);
  }
  return null;
}

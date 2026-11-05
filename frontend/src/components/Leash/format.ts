/** Plain-word labels for the Leash screens. Pure functions, no I/O. */
import type { OwnerAction, PayoutStatus } from "../../types/contracts";

/** "sb-sara@personal.example.com" -> "sb***@personal.example.com". Never log or show a full payout email in a grid. */
export function maskEmail(email: string): string {
  const at = email.lastIndexOf("@");
  if (at < 1) return "***";
  return `${email.slice(0, Math.min(2, at))}***${email.slice(at)}`;
}

const FLAG_LABELS: Record<string, string> = {
  "PR-01": "Payout email changed",
  "PR-02": "Unusual hours",
  "PR-03": "First payment",
  "PR-04": "Rate differs from roster",
  "PR-05": "Unknown or duplicate",
  "PR-06": "Over 80% of cash received",
  "PR-07": "Already paid",
  "G-01": "Small invoice, clause quoted",
  "G-02": "Invoice over the auto-send limit",
  "G-03": "Client never auto-contacted",
  "G-04": "Repeat invoice to same client",
  "G-05": "Line items don't add up",
  DEFAULT: "Not checked yet",
};

/** A short plain label for a rule or check id. Unknown ids are shown as they are. */
export function flagLabel(flag: string): string {
  return FLAG_LABELS[flag] ?? flag;
}

/** What the owner did to a payroll line. Empty when nothing yet. */
export function ownerActionLabel(action: OwnerAction | null): string {
  if (action === "APPROVED") return "Approved";
  if (action === "EXCLUDED") return "Excluded";
  return "";
}

/** PayPal Payouts item status in plain words. */
export function payoutLabel(status: PayoutStatus | null): string {
  switch (status) {
    case "PENDING":
      return "Pending";
    case "SUCCESS":
      return "Paid";
    case "UNCLAIMED":
      return "Unclaimed";
    case "FAILED":
      return "Failed";
    default:
      return "Not sent";
  }
}

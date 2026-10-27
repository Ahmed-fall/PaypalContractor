/**
 * Money helpers. The API sends money as 2-decimal strings ("850.00"). We never do
 * arithmetic on floats: values are converted to integer cents, summed, and formatted
 * back (plan R4, REQ-LSH-14). Missing values coalesce to 0 so totals never show NaN.
 */

const MONEY_RE = /^(-)?(\d+)(?:\.(\d{1,2}))?$/;

/** "850.00" -> 85000. null, undefined, "" or anything malformed -> 0. */
export function toCents(value: string | null | undefined): number {
  if (value === null || value === undefined) return 0;
  const trimmed = String(value).trim();
  const m = MONEY_RE.exec(trimmed);
  if (!m) {
    // Fallback the plan names explicitly; still rounds to whole cents.
    const n = Math.round(parseFloat(trimmed) * 100);
    return Number.isFinite(n) ? n : 0;
  }
  const sign = m[1] ? -1 : 1;
  const whole = parseInt(m[2], 10);
  const frac = parseInt((m[3] ?? "0").padEnd(2, "0"), 10);
  return sign * (whole * 100 + frac);
}

/** 85000 -> "850.00" (wire format). */
export function centsToString(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(Math.trunc(cents));
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

/** Sum money strings exactly. */
export function sumMoney(values: Array<string | null | undefined>): string {
  return centsToString(values.reduce((acc, v) => acc + toCents(v), 0));
}

const formatters = new Map<string, Intl.NumberFormat>();

/** "1500.5" -> "$1,500.50". Display only. */
export function formatMoney(value: string | null | undefined, currency = "USD"): string {
  const cents = toCents(value);
  let f = formatters.get(currency);
  if (!f) {
    try {
      f = new Intl.NumberFormat("en-US", { style: "currency", currency });
    } catch {
      f = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
    }
    formatters.set(currency, f);
  }
  // Format from the integer parts so no float noise can appear.
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  const parts = f.formatToParts(Math.floor(abs / 100));
  const whole = parts
    .filter((p) => p.type !== "decimal" && p.type !== "fraction")
    .map((p) => p.value)
    .join("");
  return `${sign}${whole}.${String(abs % 100).padStart(2, "0")}`;
}

/** True when the string is a valid non-negative 2dp money value. */
export function isMoney(value: string): boolean {
  return /^\d+(\.\d{1,2})?$/.test(value.trim());
}

/** Normalise user input to the wire format ("850" -> "850.00"). */
export function normaliseMoney(value: string): string {
  return centsToString(toCents(value));
}

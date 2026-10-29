import { describe, expect, it } from "vitest";
import { centsToString, formatMoney, normaliseMoney, sumMoney, toCents } from "../lib/money";

describe("money in integer cents (plan R4)", () => {
  it("parses wire strings exactly", () => {
    expect(toCents("850.00")).toBe(85000);
    expect(toCents("0.10")).toBe(10);
    expect(toCents("1500.5")).toBe(150050);
    expect(toCents("-12.34")).toBe(-1234);
  });

  it("coalesces null, empty and junk to zero instead of NaN", () => {
    expect(toCents(null)).toBe(0);
    expect(toCents(undefined)).toBe(0);
    expect(toCents("")).toBe(0);
    expect(toCents("abc")).toBe(0);
  });

  it("never shows float noise in totals", () => {
    // 0.1 + 0.2 and 849.99 + 0.01 are the classic float traps.
    expect(sumMoney(["0.10", "0.20"])).toBe("0.30");
    expect(sumMoney(["849.99", "0.01"])).toBe("850.00");
    expect(sumMoney(Array(10).fill("84.99"))).toBe("849.90");
    expect(sumMoney([null, undefined, "420.00"])).toBe("420.00");
  });

  it("formats for display without touching floats", () => {
    expect(formatMoney("1000.00")).toBe("$1,000.00");
    expect(formatMoney("1234567.89")).toBe("$1,234,567.89");
    expect(formatMoney(null)).toBe("$0.00");
    expect(centsToString(5)).toBe("0.05");
    expect(normaliseMoney("850")).toBe("850.00");
  });
});

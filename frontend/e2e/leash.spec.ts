import { expect, test, type Locator, type Page } from "@playwright/test";

/**
 * TST-24 (REQ-LSH-13, REQ-LSH-14): Rules -> run review -> exclude -> approve -> Pay team -> margin.
 *
 * Mock mode keeps its state in page JS, so the whole flow is driven by clicks and links:
 * no page.goto after the first load (a reload would reset the run).
 */
const DEMO_KEY = process.env.E2E_DEMO_KEY ?? "demo";

/** A line's cells: AG Grid splits a row with pinned columns into several row elements. */
const lineCell = (page: Page, lineId: string, text: string): Locator =>
  page.locator(`[row-id="${lineId}"]`).getByText(text, { exact: true }).first();

test("TST-24 Leash UI: rules, review, exclude, approve, pay team, margin", async ({ page }) => {
  test.setTimeout(60_000);
  const started = Date.now();
  await page.addInitScript((key) => sessionStorage.setItem("fd.demoKey", key), DEMO_KEY);

  // 0. The client underpays MS-01 ($850 of $1,000). In mock mode the ledger only has
  // MS-01 margin numbers once a payment has been captured in this page session.
  await page.goto("/simulator");
  await page.getByTestId("sim-milestone").selectOption("ms_01");
  await page.getByRole("radio", { name: /Too little/ }).check();
  await page.getByTestId("sim-run").click();
  await expect(page.getByText("What happened")).toBeVisible();
  await expect(page.getByTestId("sim-run")).toBeEnabled();

  // 1. Rules
  await page.getByRole("link", { name: "Rules", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Rules", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Active rules" })).toBeVisible();

  // 2. Payroll -> open the run
  await page.getByRole("link", { name: "Payroll", exact: true }).click();
  await page.getByRole("link", { name: "Open run prn_0001" }).click();
  await expect(page.getByText("Payout email changed", { exact: true })).toBeVisible();
  await expect(page.getByText("$720.00", { exact: true }).first()).toBeVisible();
  const payTeam = page.getByRole("button", { name: "Pay team" });
  await expect(payTeam).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: "Approve: Over 80% of cash received" })).toBeVisible();

  // 3. Exclude Omar
  await page.getByRole("button", { name: "Exclude Omar" }).click();
  await expect(page.getByText("$420.00", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("checkbox", { name: /Over 80% of cash received/ })).toHaveCount(0);
  await expect(payTeam).toBeEnabled();

  // 4. Approve him back: the total returns and the 80% check is back
  await page.getByRole("button", { name: "Approve Omar" }).click();
  await expect(page.getByText("$720.00", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "Approve: Over 80% of cash received" })).toBeVisible();
  await expect(payTeam).toBeDisabled();

  // 5. Exclude him again: the demo story, Omar is not paid
  await page.getByRole("button", { name: "Exclude Omar" }).click();
  await expect(page.getByText("$420.00", { exact: true }).first()).toBeVisible();
  await expect(payTeam).toBeEnabled();

  // 6. Pay team -> confirm
  await payTeam.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Send $420.00 to 2 people through PayPal Payouts? This can't be undone.")).toBeVisible();
  await dialog.getByRole("button", { name: "Send payment" }).click();

  // 7. Sent, then the 2nd 3 s poll lands PAID
  await expect(page.getByText("Sent to PayPal. Batch 5UXD2E8A7EBQJ")).toBeVisible();
  await expect(page.getByText("Paid: 2 people, $420.00")).toBeVisible({ timeout: 15_000 });
  await expect(lineCell(page, "prl_0002", "Paid")).toBeVisible(); // Sara
  await expect(lineCell(page, "prl_0003", "Paid")).toBeVisible(); // Youssef

  // 8. Ledger margin for MS-01 (fixture: shared/fixtures/leash/ledger_margin.json)
  await page.getByRole("link", { name: "Ledger", exact: true }).click();
  const summary = page.getByRole("region", { name: "Reconciliation summary" });
  await expect(summary.getByText("Received on PayPal")).toBeVisible();
  await expect(summary.getByText("Invoiced", { exact: true })).toBeVisible();
  await expect(summary.getByText("Paid to team")).toBeVisible();
  await expect(summary.getByText("Margin kept")).toBeVisible();
  await expect(summary.getByText("$200.00", { exact: true })).toBeVisible(); // invoiced_open
  await expect(summary.getByText("$420.00", { exact: true })).toBeVisible(); // paid_to_team
  await expect(summary.getByText("$430.00", { exact: true })).toBeVisible(); // margin
  // The summary's "Received" is all contracts ($6,050.00); MS-01's own cash_in ($850.00) is in the grid.
  await expect(page.getByRole("gridcell", { name: "$850.00" }).first()).toBeVisible();
  await expect(summary.getByText("Not available")).toHaveCount(0);

  // 9. Budget
  const elapsed = Date.now() - started;
  console.log(`TST-24 elapsed ${elapsed} ms`);
  expect(elapsed).toBeLessThan(60_000);
});

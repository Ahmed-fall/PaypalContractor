import { expect, test, type Locator, type Page } from "@playwright/test";

/**
 * TST-24 (REQ-LSH-13, REQ-LSH-14): Rules -> run review -> exclude -> approve -> Pay team -> margin.
 *
 * Two modes:
 * - mock (default): Vite with VITE_USE_MOCKS=true. Mock state lives in page JS, so the whole
 *   flow is driven by clicks and links: no page.goto after the first load.
 * - live: E2E_BASE_URL (the web app) + E2E_API_URL (the API origin) + E2E_DEMO_KEY. The test
 *   first resets the demo through POST /api/v1/demo/reset, then runs against the real sandbox.
 *   PayPal settles a Payouts batch in ~30-60 s, so the 60 s budget counts the owner's clicks;
 *   the wait for "Paid" is reported separately.
 */
const LIVE = Boolean(process.env.E2E_BASE_URL);
const DEMO_KEY = process.env.E2E_DEMO_KEY ?? "demo";
const API_URL = process.env.E2E_API_URL ?? "";
const MILESTONE_ID = process.env.E2E_MILESTONE_ID ?? (LIVE ? "ms_seed_01" : "ms_01");
const SETTLE_MS = LIVE ? 150_000 : 15_000;

/** A line's row id, from the pinned Member cell that shows the person's name. */
async function lineId(page: Page, member: string): Promise<string> {
  const row = page.locator("[row-id]").filter({ hasText: member }).first();
  const id = await row.getAttribute("row-id");
  expect(id, `row for ${member}`).toBeTruthy();
  return id as string;
}

/** A line's cell: AG Grid splits a row with pinned columns into several row elements. */
const lineCell = (page: Page, id: string, text: string): Locator =>
  page.locator(`[row-id="${id}"]`).getByText(text, { exact: true }).first();

test("TST-24 Leash UI: rules, review, exclude, approve, pay team, margin", async ({ page, request }) => {
  test.setTimeout(LIVE ? 240_000 : 60_000);
  if (LIVE) {
    expect(API_URL, "E2E_API_URL is required in live mode").toBeTruthy();
    const reset = await request.post(`${API_URL}/api/v1/demo/reset`, { headers: { "X-Demo-Key": DEMO_KEY } });
    expect(reset.status(), await reset.text()).toBe(200);
  }
  const started = Date.now();
  let waited = 0;
  await page.addInitScript((key) => sessionStorage.setItem("fd.demoKey", key), DEMO_KEY);

  // 0. The client underpays MS-01 ($850 of $1,000, five days late).
  await page.goto("/simulator");
  await page.getByTestId("sim-milestone").selectOption(MILESTONE_ID);
  await page.getByRole("radio", { name: /Too little/ }).check();
  await page.getByTestId("sim-run").click();
  // The real capture + audit + invoice takes several seconds live: wait for the request to end.
  await expect(page.getByTestId("sim-run")).toBeEnabled({ timeout: LIVE ? 60_000 : 5_000 });
  await expect(page.getByText("What happened")).toBeVisible();

  // 1. Rules
  await page.getByRole("link", { name: "Rules", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Rules", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Active rules" })).toBeVisible();

  // 2. Payroll -> open the run (H2 checks it in the background; Leash flags appear once checked)
  await page.getByRole("link", { name: "Payroll", exact: true }).click();
  await page.getByRole("link", { name: /^Open run prn_/ }).first().click();
  await expect(page.getByText("Payout email changed", { exact: true })).toBeVisible({ timeout: 15_000 });
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
  const sara = await lineId(page, "Sara");
  const youssef = await lineId(page, "Youssef");

  // 6. Pay team -> confirm
  await payTeam.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Send $420.00 to 2 people through PayPal Payouts? This can't be undone.")).toBeVisible();
  await dialog.getByRole("button", { name: "Send payment" }).click();

  // 7. Sent, then the page's 3 s polling shows PayPal's result
  await expect(page.getByText(/^Sent to PayPal\. Batch [A-Z0-9]+$/)).toBeVisible({ timeout: LIVE ? 30_000 : 5_000 });
  const clicks = Date.now() - started;
  const settleStart = Date.now();
  await expect(page.getByText("Paid: 2 people, $420.00")).toBeVisible({ timeout: SETTLE_MS });
  waited = Date.now() - settleStart;
  await expect(lineCell(page, sara, "Paid")).toBeVisible();
  await expect(lineCell(page, youssef, "Paid")).toBeVisible();

  // 8. Ledger margin for MS-01: received 850, invoiced 200, paid to team 420, margin 430
  await page.getByRole("link", { name: "Ledger", exact: true }).click();
  const summary = page.getByRole("region", { name: "Reconciliation summary" });
  await expect(summary.getByText("Received on PayPal")).toBeVisible();
  await expect(summary.getByText("Invoiced", { exact: true })).toBeVisible();
  await expect(summary.getByText("Paid to team")).toBeVisible();
  await expect(summary.getByText("Margin kept")).toBeVisible();
  // Live, the summary's headline sentence repeats some figures, so take the first match.
  await expect(summary.getByText("$200.00", { exact: true }).first()).toBeVisible(); // invoiced_open
  await expect(summary.getByText("$420.00", { exact: true }).first()).toBeVisible(); // paid_to_team
  await expect(summary.getByText("$430.00", { exact: true }).first()).toBeVisible(); // margin
  await expect(page.getByRole("gridcell", { name: "$850.00" }).first()).toBeVisible();
  await expect(summary.getByText("Not available")).toHaveCount(0);

  // 9. Budget: the owner's clicks (start -> "Sent to PayPal") stay under 60 s
  const total = Date.now() - started;
  console.log(`TST-24 ${LIVE ? "live" : "mock"}: clicks ${clicks} ms, PayPal settle ${waited} ms, total ${total} ms`);
  expect(clicks).toBeLessThan(60_000);
  if (!LIVE) expect(total).toBeLessThan(60_000);
});

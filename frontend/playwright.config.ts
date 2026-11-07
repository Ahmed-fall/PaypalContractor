import { defineConfig, devices } from "@playwright/test";

// TST-24. Default: Vite dev server in mock mode on :5180.
// Live mode: E2E_BASE_URL=https://<render-host> (skips the webServer). The demo key
// then comes from E2E_DEMO_KEY, and the data must match the mock story (run prn_0001).
const PORT = 5180;
const live = Boolean(process.env.E2E_BASE_URL);

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? `http://localhost:${PORT}`,
    viewport: { width: 1920, height: 1080 },
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1920, height: 1080 } } }],
  webServer: live
    ? undefined
    : {
        command: `npx vite --port ${PORT} --strictPort`,
        url: `http://localhost:${PORT}`,
        env: { VITE_USE_MOCKS: "true" },
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
});

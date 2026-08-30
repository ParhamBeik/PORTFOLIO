import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright against the Vite dev server (:5173).
 *
 * Required for authenticated specs:
 *   E2E_EMAIL=you@example.com
 *   E2E_PASSWORD=...
 *
 * The API throttles anonymous requests (30/min) and every spec signs in from
 * scratch, so a full run exhausts that allowance about a third of the way
 * through and the rest fail to sign in. Give the backend a headroom the suite
 * cannot hit, or most of it never runs:
 *
 *   ANON_THROTTLE=10000/min USER_THROTTLE=10000/min   (on the Django process)
 *
 * Optional:
 *   VITE_PROXY_TARGET / VITE_API_URL — Django API (default http://localhost:8000)
 *
 * Specs soft-skip when the app or backend is unavailable so CI can still parse.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 90 * 1000,
  expect: { timeout: 5000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.PLAYWRIGHT_TEST_BASE_URL || process.env.BASE_URL || "http://localhost:5173",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  webServer: (process.env.PLAYWRIGHT_TEST_BASE_URL || process.env.BASE_URL) ? undefined : {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 120 * 1000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});

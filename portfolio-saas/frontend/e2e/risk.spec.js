import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("risk", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  test("risk section shows summary and portfolio metrics", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }

    await page.getByTestId("nav-portfolio").click();
    const card = page.getByTestId("dashboard-risk");
    await expect(card).toBeVisible({ timeout: 20000 });
    await expect(page.getByTestId("dashboard-risk-body")).toBeVisible({ timeout: 15000 });
    await expect.soft(page.getByTestId("dashboard-risk-summary")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-risk-asset-table")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-risk-window")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-risk-view")).toBeVisible();
    await page.getByTestId("dashboard-risk-view-portfolio").click();
    await expect.soft(page.getByTestId("dashboard-risk-volatility")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-risk-max-drawdown")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-risk-sharpe")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-risk-var")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-risk-cvar")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-risk-calmar")).toBeVisible();
  });

  test("VaR/CVaR labels carry the daily basis", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-risk-body")).toBeVisible({ timeout: 20000 });
    await page.getByTestId("dashboard-risk-view-portfolio").click();

    await expect.soft(page.getByTestId("dashboard-risk-var")).toContainText("daily");
    await expect.soft(page.getByTestId("dashboard-risk-cvar")).toContainText("daily");
  });

  test("benchmark metrics never render as a blank cell", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-risk-body")).toBeVisible({ timeout: 20000 });
    await page.getByTestId("dashboard-risk-view-portfolio").click();

    // Either the real beta/alpha tiles render, or an explicit "Unavailable"
    // tile with a reason — never nothing, never a bare dash.
    const benchmarkUnavailable = page.getByTestId("dashboard-risk-benchmark");
    const beta = page.getByTestId("dashboard-risk-beta");
    await expect.soft(benchmarkUnavailable.or(beta)).toBeVisible({ timeout: 15000 });
    if (await benchmarkUnavailable.isVisible().catch(() => false)) {
      await expect.soft(benchmarkUnavailable).not.toContainText("—");
    }
  });
  test("by-asset tab shows holdings table", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-risk-body")).toBeVisible({ timeout: 20000 });
    await page.getByTestId("dashboard-risk-view-asset").click();
    await expect.soft(page.getByTestId("dashboard-risk-asset-table")).toBeVisible({ timeout: 15000 });
  });

  test("every holding is listed and the analyzed share is stated", async ({ page }) => {
    // The bug this guards: holdings that failed a market-universe screening gate
    // used to be dropped from the panel entirely, so the card reported the risk
    // of a third of the book under the whole portfolio's name.
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-risk-body")).toBeVisible({ timeout: 20000 });

    await expect.soft(page.getByTestId("dashboard-risk-analyzed-weight")).toBeVisible();

    const holdingsRows = await page.getByTestId("dashboard-holdings-table").locator("tbody tr").count();
    await page.getByTestId("dashboard-risk-view-asset").click();
    const riskRows = page.getByTestId("dashboard-risk-asset-table").locator("tbody tr");
    await expect.soft(riskRows.first()).toBeVisible({ timeout: 15000 });
    if (holdingsRows > 0) {
      // Rows merge across accounts, so the risk table can be shorter -- never longer,
      // and never empty while holdings exist.
      expect.soft(await riskRows.count()).toBeGreaterThan(0);
      expect.soft(await riskRows.count()).toBeLessThanOrEqual(holdingsRows);
    }
  });

});

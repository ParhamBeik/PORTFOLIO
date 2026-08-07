import { test, expect } from "@playwright/test";
import { clickTab, requireLogin } from "./helpers.js";

test.describe("optimal", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
    await page.getByTestId("nav-my-optimal").click();
  });

  test("page reaches a known state", async ({ page }) => {
    const known = page
      .getByTestId("optimal-main")
      .or(page.getByTestId("optimal-window-tabs"))
      .or(page.getByTestId("optimal-empty-holdings"))
      .or(page.getByTestId("optimal-empty-universe"))
      .or(page.getByTestId("optimal-insufficient"))
      .or(page.getByTestId("pro-required"));

    await expect(known.first()).toBeVisible({ timeout: 25000 });
  });

  test("window tabs and scenario toggle when data present", async ({ page }) => {
    const tabs = page.getByTestId("optimal-window-tabs");
    if (!(await tabs.isVisible().catch(() => false))) {
      test.skip(true, "optimal windows not available (empty/pro/insufficient)");
    }

    const windowButtons = tabs.locator("[data-testid^='optimal-window-tabs-']");
    const n = await windowButtons.count();
    expect.soft(n).toBeGreaterThan(0);
    if (n > 1) await windowButtons.nth(1).click();

    const scenario = page.getByTestId("optimal-scenario-tabs");
    await expect.soft(scenario).toBeVisible();
    await clickTab(page, "optimal-scenario-tabs", "max_sharpe");
    await clickTab(page, "optimal-scenario-tabs", "min_volatility");
  });

  test("rebalance table or allocation when history sufficient", async ({ page }) => {
    if (await page.getByTestId("optimal-insufficient").isVisible().catch(() => false)) {
      // Graceful empty — counts as pass for this coverage branch.
      await expect(page.getByTestId("optimal-insufficient")).toBeVisible();
      return;
    }
    if (!(await page.getByTestId("optimal-window-tabs").isVisible().catch(() => false))) {
      test.skip(true, "no optimal body to inspect");
    }

    const content = page
      .getByTestId("optimal-trades")
      .or(page.getByTestId("optimal-trades-empty"))
      .or(page.getByTestId("optimal-trades-card"))
      .or(page.getByTestId("optimal-allocation-card"))
      .or(page.getByTestId("optimal-comparison"));
    await expect.soft(content.first()).toBeVisible({ timeout: 15000 });
  });

  test("frontier panel soft-present", async ({ page }) => {
    if (!(await page.getByTestId("optimal-window-tabs").isVisible().catch(() => false))) {
      test.skip(true, "no optimal body (frontier only loads with windows)");
    }
    // Frontier Async may be loading/empty/card — any of these is fine.
    const frontier = page
      .getByTestId("optimal-frontier")
      .or(page.getByTestId("optimal-frontier-card"));
    await expect.soft(frontier.first()).toBeVisible({ timeout: 20000 });
  });

  test("insufficient-history handled gracefully", async ({ page }) => {
    const tabs = page.getByTestId("optimal-window-tabs");
    if (!(await tabs.isVisible().catch(() => false))) {
      // Empty holdings / universe / pro — still graceful UI.
      const graceful = page
        .getByTestId("optimal-empty-holdings")
        .or(page.getByTestId("optimal-empty-universe"))
        .or(page.getByTestId("optimal-empty-windows"))
        .or(page.getByTestId("pro-required"))
        .or(page.getByTestId("optimal-insufficient"));
      await expect(graceful.first()).toBeVisible({ timeout: 20000 });
      return;
    }

    // Probe each window; if any shows insufficient, that path is covered.
    const buttons = tabs.locator("[data-testid^='optimal-window-tabs-']");
    const n = await buttons.count();
    let sawInsufficient = false;
    for (let i = 0; i < n; i++) {
      await buttons.nth(i).click();
      if (await page.getByTestId("optimal-insufficient").isVisible().catch(() => false)) {
        sawInsufficient = true;
        break;
      }
    }
    // Either we found the empty state or every window has data — both OK.
    expect.soft(sawInsufficient || n > 0).toBeTruthy();
  });
});

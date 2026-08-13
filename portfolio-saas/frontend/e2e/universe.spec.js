import { test, expect } from "@playwright/test";
import { clickTab, requireLogin } from "./helpers.js";

test.describe("universe", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
    await page.getByTestId("nav-best-overall").click();
  });

  test("page reaches computed or empty state", async ({ page }) => {
    const known = page
      .getByTestId("universe-best")
      .or(page.getByTestId("universe-not-computed"))
      .or(page.getByTestId("universe-window"));

    await expect(known.first()).toBeVisible({ timeout: 25000 });
  });

  test("not-computed empty state is graceful", async ({ page }) => {
    const empty = page.getByTestId("universe-not-computed");
    const windows = page.getByTestId("universe-window");

    await expect(empty.or(windows).first()).toBeVisible({ timeout: 25000 });
    if (await empty.isVisible().catch(() => false)) {
      await expect(empty).toBeVisible();
    }
  });

  test("window tabs / weights / leaders / gap when computed", async ({ page }) => {
    if (await page.getByTestId("universe-not-computed").isVisible().catch(() => false)) {
      test.skip(true, "nightly best-overall not computed yet");
    }
    const windowTabs = page.getByTestId("universe-window");
    if (!(await windowTabs.isVisible().catch(() => false))) {
      test.skip(true, "universe windows not available");
    }

    const windowButtons = windowTabs.locator("[data-testid^='universe-window-']");
    expect.soft(await windowButtons.count()).toBeGreaterThan(0);
    if ((await windowButtons.count()) > 1) await windowButtons.nth(0).click();

    await expect.soft(page.getByTestId("universe-scenario")).toBeVisible();
    await clickTab(page, "universe-scenario", "max_sharpe");

    // Either window-empty or full panels.
    if (await page.getByTestId("universe-window-empty").isVisible().catch(() => false)) {
      await expect(page.getByTestId("universe-window-empty")).toBeVisible();
    } else {
      await expect.soft(page.getByTestId("universe-weights-card")).toBeVisible({ timeout: 15000 });
      await expect
        .soft(
          page
            .getByTestId("universe-weights-donut")
            .or(page.getByTestId("universe-weights-table"))
            .or(page.getByTestId("universe-weights-table-empty"))
        )
        .toBeVisible();
      await expect.soft(page.getByTestId("universe-gap-card")).toBeVisible();
    }

    await expect.soft(page.getByTestId("universe-leaders-card")).toBeVisible();
    const leadersBody = page
      .getByTestId("universe-leaders-empty")
      .or(page.locator("[data-testid^='universe-leaders-']:not([data-testid='universe-leaders-card']):not([data-testid='universe-leaders-empty'])").first());
    await expect.soft(leadersBody).toBeVisible();
  });
});

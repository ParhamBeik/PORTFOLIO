import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("ops center", () => {
  test("staff sees Ops nav and overview; non-staff cannot stay on /ops", async ({ page }) => {
    await requireLogin(page, test);
    const opsNav = page.getByTestId("nav-ops");
    const visible = await opsNav.isVisible().catch(() => false);
    if (!visible) {
      await page.goto("/ops");
      await expect(page).not.toHaveURL(/\/ops$/);
      return;
    }
    await opsNav.click();
    await expect(page.getByTestId("ops-page")).toBeVisible({ timeout: 20000 });
    await expect(page.getByTestId("ops-refresh")).toBeVisible();
    await expect(page.getByTestId("ops-data-age")).toBeVisible();
    await expect(page.getByTestId("ops-data-age")).toContainText(/Data age: \d+s/);
    await expect(page.getByTestId("ops-tabs")).toBeVisible();
    await expect(page.getByTestId("ops-overview")).toBeVisible();
    await page.getByTestId("ops-tabs-live").click();
    await expect(page.getByTestId("ops-live-held")).toBeVisible();
    await page.getByTestId("ops-tabs-warehouse").click();
    await expect(page.getByTestId("ops-warehouse")).toBeVisible();
    await expect(page.getByTestId("ops-warehouse-census")).toBeVisible();
    await expect(page.getByTestId("ops-warehouse-census-fetched")).toBeVisible();
    await page.getByTestId("ops-tabs-tables").click();
    await expect(page.getByTestId("ops-tables")).toBeVisible();
    await page.getByTestId("ops-tabs-jobs").click();
    await expect(page.getByTestId("ops-jobs-health-panel")).toBeVisible();
    await expect(page.getByTestId("ops-jobs-health-summary")).toBeVisible();
    await expect(page.getByTestId("ops-workflows-panel")).toBeVisible();
    await expect(page.getByTestId("ops-errors-panel")).toBeVisible();
    await expect(page.getByTestId("ops-archives-panel")).toBeVisible();
    await page.getByTestId("ops-tabs-asset").click();
    await expect(page.getByTestId("ops-asset-search")).toBeVisible();
    await expect(page.getByTestId("ops-asset-catalog")).toBeVisible();
    await expect(page.getByTestId("ops-asset-class-all")).toBeVisible();
    await page.getByTestId("ops-tabs-codal").click();
    await expect(page.getByTestId("ops-codal")).toBeVisible();
  });
});

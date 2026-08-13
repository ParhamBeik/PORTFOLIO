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
    await expect(page.getByTestId("ops-tabs")).toBeVisible();
    await expect(page.getByTestId("ops-status")).toBeVisible();
    await page.getByTestId("ops-tabs-completeness").click();
    await expect(page.getByTestId("ops-completeness")).toBeVisible();
    await page.getByTestId("ops-tabs-asset").click();
    await expect(page.getByTestId("ops-asset-search")).toBeVisible();
    await expect(page.getByTestId("ops-asset-load")).toBeVisible();
    await page.getByTestId("ops-tabs-codal").click();
    await expect(page.getByTestId("ops-codal")).toBeVisible();
  });
});

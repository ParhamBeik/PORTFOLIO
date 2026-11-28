import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("four-destination app", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  test("shows Home, Activity, Research, and Compare only", async ({ page }) => {
    for (const destination of ["home", "activity", "research", "compare"]) {
      await expect(page.getByTestId(`nav-${destination}`)).toBeVisible();
    }
    await expect(page.getByTestId("nav").locator("a")).toHaveCount(4);
    await page.getByTestId("nav-activity").click();
    await expect(page.getByTestId("activity-section")).toBeVisible();
    await page.getByTestId("nav-research").click();
    await expect(page.getByTestId("research-section")).toBeVisible();
  });

  test("legacy /risk redirects to Compare", async ({ page }) => {
    await page.goto("/risk");
    await expect(page).toHaveURL(/\/compare$/);
  });

  test("ordinary users cannot open Operations", async ({ page }) => {
    await expect(page.getByTestId("nav-ops")).toHaveCount(0);
    await page.goto("/ops");
    await expect(page).not.toHaveURL(/\/ops$/);
  });
});

import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("five-destination app", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  test("shows Home, Activity, Research, Compare, and Risk only", async ({ page }) => {
    for (const destination of ["home", "activity", "research", "compare", "risk"]) {
      await expect(page.getByTestId(`nav-${destination}`)).toBeVisible();
    }
    await expect(page.getByTestId("nav").locator("a")).toHaveCount(5);
    await page.getByTestId("nav-activity").click();
    await expect(page.getByTestId("activity-section")).toBeVisible();
    await page.getByTestId("nav-research").click();
    await expect(page.getByTestId("research-section")).toBeVisible();
  });

  test("Guidance gives a profile and discloses its data window", async ({ page }) => {
    test.setTimeout(120000);
    await page.getByTestId("nav-risk").click();
    await expect(page.getByTestId("guidance-risk-profile")).toHaveValue("balanced");
    await expect(page.getByTestId("guidance-window")).toBeVisible({ timeout: 90000 });
    await expect(
      page.getByTestId("guidance-personal").or(page.getByTestId("guidance-personal-empty"))
    ).toBeVisible();
    await expect(
      page.getByTestId("guidance-benchmark").or(page.getByTestId("guidance-benchmark-empty"))
    ).toBeVisible();
  });

  test("ordinary users cannot open Operations", async ({ page }) => {
    await expect(page.getByTestId("nav-ops")).toHaveCount(0);
    await page.goto("/ops");
    await expect(page).not.toHaveURL(/\/ops$/);
  });
});

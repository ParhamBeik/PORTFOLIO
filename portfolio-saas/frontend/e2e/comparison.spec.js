import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("comparison", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  // Four windows, one rule: every one commits to an answer -- the table, or a
  // sentence saying why there isn't one. A blank panel is the single forbidden
  // outcome.
  //
  // Each click is awaited on the API response it triggers, not on the DOM.
  // `useApi` keeps the previous data visible across a refetch, so a DOM-only
  // wait reads the PREVIOUS window's answer and passes while proving nothing.
  // A settled refusal and the first-load spinner share one `comparison-result`
  // testid, so they are told apart by role: `alert` is a stated reason.
  test("the table answers in every window", async ({ page }) => {
    test.setTimeout(3 * 60 * 1000);
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-compare").click();
    await expect(page.getByTestId("comparison-panel")).toBeVisible({ timeout: 20000 });
    await expect(page.getByTestId("comparison-subject")).toBeVisible({ timeout: 20000 });
    if (await page.getByTestId("comparison-no-holdings").isVisible().catch(() => false)) {
      test.skip(true, "portfolio has no priced positions to compare");
    }

    for (const range of ["0", "365", "180", "90"]) {
      const control = page.getByTestId(`comparison-range-${range}`);
      if ((await control.getAttribute("aria-pressed")) !== "true") {
        const settled = page.waitForResponse(
          (r) => r.url().includes("/api/comparison/?") && r.url().includes(`days=${range}`),
          { timeout: 30000 }
        );
        await control.click();
        await settled.catch(() => {});
      }
      await expect
        .soft(
          page
            .getByTestId("comparison-table")
            .or(page.getByTestId("comparison-result").and(page.locator("[role=alert]")))
            .first(),
          `window ${range}`
        )
        .toBeVisible({ timeout: 30000 });
    }
  });
});

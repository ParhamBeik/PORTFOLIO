import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("history and import surfaces", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
  });

  test("shows a single-asset history answer", async ({ page }) => {
    await page.getByTestId("nav-markets").click();
    await expect(page.getByTestId("asset-history-card")).toBeVisible({ timeout: 20000 });
    await expect(
      page.getByTestId("asset-history-result").or(page.getByTestId("asset-history-empty")).first()
    ).toBeVisible({ timeout: 30000 });
  });

  test("validates a CSV before offering import", async ({ page }) => {
    // Import is a single-account action, so the scope has to be one portfolio
    // before the control exists. Left on "All portfolios" this spec could only
    // ever skip, which is not coverage.
    const scope = page.getByTestId("scope-account");
    const ids = await scope.locator("option").evaluateAll((options) =>
      options.map((option) => option.value).filter(Boolean)
    );
    if (!ids.length) test.skip(true, "no portfolio to import into");
    await scope.selectOption(ids[0]);
    await page.getByTestId("nav-activity").click();
    const importButton = page.getByTestId("ledger-import");
    await importButton.click();
    // A deposit, not an opening entry: preview runs the real ledger rules in a
    // rolled-back transaction, and every opening must share the account's
    // tracking-start timestamp -- which a demo account already has.
    const csv = [
      "external_id,occurred_at,kind,asset_key,quantity,unit_price_tomans,amount_tomans,note",
      `e2e-cash,${new Date(Date.now() - 86400000).toISOString()},deposit,,,,1000,E2E preview`,
    ].join("\n");
    await page.getByTestId("ledger-import-file").setInputFiles({
      name: "e2e-preview.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(csv),
    });
    await page.getByTestId("ledger-import-preview").click();
    await expect(page.getByTestId("ledger-import-preview-result")).toContainText("1 rows", {
      timeout: 30000,
    });
    await expect(page.getByTestId("ledger-import-commit")).toBeEnabled();
  });
});

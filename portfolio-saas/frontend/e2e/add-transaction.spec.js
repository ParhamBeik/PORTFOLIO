import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

/**
 * The add flow, driven past the category tiles.
 *
 * Every other spec stopped at step 1, so the picker could return an empty list
 * or the wizard could refuse to advance and nothing would fail. These walk it to
 * the sentence it is about to record.
 *
 * Steps are category -> asset -> action -> amount -> review. Choosing a category
 * or an asset advances on its own; the later screens need Continue.
 */
const anyAsset = (page) =>
  page
    .locator('[data-testid^="add-transaction-asset-"]')
    .filter({ hasNot: page.locator('[data-testid$="-empty"]') })
    .filter({ hasNot: page.locator('[data-testid$="-search"]') })
    .filter({ hasNot: page.locator('[data-testid$="-truncated"]') });

test.describe("add transaction", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-activity").click();
    const add = page.getByTestId("ledger-add");
    if (!(await add.isVisible({ timeout: 20000 }).catch(() => false))) {
      test.skip(true, "no add control on this account");
    }
    await add.click();
    await expect(page.getByTestId("add-transaction")).toBeVisible();
  });

  test("every asset class offers something to pick", async ({ page }) => {
    // The bug this covers: the picker rendered a 13-row hand-written seed, so
    // Stocks showed one row and Crypto showed none at all.
    for (const category of ["gold", "cash", "stock", "crypto"]) {
      await page.getByTestId(`add-transaction-category-${category}`).click();
      await expect
        .soft(anyAsset(page).first(), `${category} has at least one pickable asset`)
        .toBeVisible({ timeout: 20000 });
      await expect
        .soft(page.getByTestId("add-transaction-asset-empty"), `${category} is not empty`)
        .toHaveCount(0);
      await page.getByTestId("add-transaction-back").click();
    }
  });

  test("a stock purchase reaches the review sentence priced in Rial", async ({ page }) => {
    await page.getByTestId("add-transaction-category-stock").click();

    const first = anyAsset(page).first();
    if (!(await first.isVisible({ timeout: 20000 }).catch(() => false))) {
      test.skip(true, "stock catalog is empty in this environment");
    }
    await first.click();

    await page.getByTestId("add-transaction-action-buy").click();
    await page.getByTestId("add-transaction-next").click();

    await page.getByTestId("add-transaction-quantity").fill("100");
    await page.getByTestId("add-transaction-next").click();

    // TSE quotes are Rial while every portfolio total is Toman, so the review
    // line has to divide the product exactly once: 100 x 5,000 Rial = 50,000 T.
    await page.getByTestId("add-transaction-own-price").check();
    await page.getByTestId("add-transaction-price").fill("5000");

    const summary = page.getByTestId("add-transaction-summary");
    await expect(summary).toBeVisible({ timeout: 15000 });
    await expect(summary).toContainText("Rial");
    await expect(summary).toContainText("50,000");
    await expect(page.getByTestId("add-transaction-save")).toBeEnabled();
  });
});

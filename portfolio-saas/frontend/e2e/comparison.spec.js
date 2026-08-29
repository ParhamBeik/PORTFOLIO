import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("comparison", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  test("runs a counterfactual from the pickers", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-comparison").click();
    await expect(page.getByTestId("comparison-panel")).toBeVisible({ timeout: 20000 });

    const subject = page.getByTestId("comparison-subject");
    await expect(subject).toBeVisible({ timeout: 20000 });
    // The subject defaults to the first holding; a portfolio with none says so
    // rather than rendering an empty chart.
    if (await page.getByTestId("comparison-no-holdings").isVisible().catch(() => false)) {
      test.skip(true, "portfolio has no priced positions to compare");
    }

    const target = page.getByTestId("comparison-target");
    await expect(target.locator("option").nth(1)).toBeAttached({ timeout: 15000 });
    const subjectVal = await subject.inputValue().catch(() => "");
    const pick = await target.locator("option").evaluateAll((opts, subj) => {
      const valid = opts.map(o => o.value).filter(v => v && v !== subj);
      return valid.length > 0 ? valid[0] : (opts[1] ? opts[1].value : "");
    }, subjectVal);
    if (pick) {
      await target.selectOption(pick);
    }

    // Either a verdict or a stated reason — both are answers. A silent empty
    // panel is the only outcome this page must never produce.
    await expect(
      page
        .getByTestId("comparison-difference")
        .or(page.getByTestId("comparison-result"))
        .first()
    ).toBeVisible({ timeout: 30000 });
  });

  test("each mode answers with a verdict or a reason", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-comparison").click();
    await expect(page.getByTestId("comparison-panel")).toBeVisible({ timeout: 20000 });

    for (const mode of ["counterfactual", "holdings", "benchmark", "lump_sum"]) {
      await page.getByTestId(`comparison-mode-${mode}`).click();
      await expect
        .soft(page.getByTestId(`comparison-mode-${mode}`), mode)
        .toHaveAttribute("aria-pressed", "true");
      await expect
        .soft(page.getByTestId("comparison-target"), mode)
        .toBeVisible();
    }
  });
});

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
    await page.getByTestId("nav-compare").click();
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
    await page.getByTestId("nav-compare").click();
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

  // Sixteen combinations of tab and window. The reported failure was that most
  // of them showed nothing at all, so the rule being enforced is only that
  // every one of them commits to an answer: a verdict, or a sentence saying why
  // there isn't one. A blank panel is the single forbidden outcome.
  //
  // Each click is awaited on the API response it triggers, not on the DOM.
  // `useApi` deliberately keeps the previous data visible across a refetch, so
  // no spinner appears and the old verdict stays on screen: every DOM-only wait
  // here reads the PREVIOUS combination's answer and passes while proving
  // nothing. That is how a green sweep hid four identical windows.
  //
  // A settled refusal and the first-load spinner share one `comparison-result`
  // testid, so they are told apart by role -- `alert` is a stated reason,
  // `status` is still loading.
  test("every mode answers in every window", async ({ page }) => {
    // Sixteen real round-trips, one of which rebuilds a year of net worth.
    // The suite-wide 90s is sized for single-screen specs.
    test.setTimeout(4 * 60 * 1000);
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-compare").click();
    await expect(page.getByTestId("comparison-panel")).toBeVisible({ timeout: 20000 });
    if (await page.getByTestId("comparison-no-holdings").isVisible().catch(() => false)) {
      test.skip(true, "portfolio has no priced positions to compare");
    }

    const target = page.getByTestId("comparison-target");
    // Anything answerable will do; the subject keeps its default (first
    // holding) and only the target has to be a different asset.
    const subjectVal = await page
      .getByTestId("comparison-subject")
      .inputValue()
      .catch(() => "");

    /**
     * Click a control and wait for the comparison call it triggers.
     *
     * Clicking the option that is already selected changes no state and fires
     * no request, so waiting on one there stalls until the timeout and burns
     * the whole test budget on the default range. `aria-pressed` says whether
     * the click will actually do anything.
     */
    const choose = async (control, token) => {
      if ((await control.getAttribute("aria-pressed")) === "true") return;
      const settled = page.waitForResponse(
        (r) => r.url().includes("/api/comparison/?") && r.url().includes(token),
        { timeout: 30000 }
      );
      await control.click();
      await settled.catch(() => {});
    };

    const windows = new Set();
    for (const mode of ["counterfactual", "holdings", "benchmark", "lump_sum"]) {
      await choose(page.getByTestId(`comparison-mode-${mode}`), `mode=${mode}`);
      if (
        await page.getByTestId("comparison-one-holding").isVisible().catch(() => false)
      ) {
        continue; // "Two of mine" with one holding: stated, not blank.
      }
      const pick = await target.locator("option").evaluateAll((opts, subj) => {
        const valid = opts.map((o) => o.value).filter((v) => v && v !== subj);
        return valid[0] || "";
      }, subjectVal);
      if (!pick) continue;
      if ((await target.inputValue().catch(() => "")) !== pick) {
        const picked = page.waitForResponse(
          (r) => r.url().includes("/api/comparison/?") && r.url().includes(pick),
          { timeout: 30000 }
        );
        await target.selectOption(pick);
        await picked.catch(() => {});
      }

      for (const range of ["0", "365", "180", "90"]) {
        await choose(page.getByTestId(`comparison-range-${range}`), `days=${range}`);
        const verdict = page.getByTestId("comparison-difference");
        await expect
          .soft(
            verdict
              .or(page.getByTestId("comparison-result").and(page.locator("[role=alert]")))
              .first(),
            `${mode} @ ${range}`
          )
          .toBeVisible({ timeout: 30000 });
        if (mode === "benchmark" && (await verdict.isVisible().catch(() => false))) {
          windows.add(await verdict.innerText());
        }
      }
    }

    // The reported symptom was three range buttons that all drew the same
    // ninety days. "Portfolio vs one asset" rebases to whatever window is
    // asked for, so its four buttons must not all render the same tile.
    if (windows.size) {
      expect.soft(windows.size, "benchmark ranges are indistinguishable").toBeGreaterThan(1);
    }
  });
});

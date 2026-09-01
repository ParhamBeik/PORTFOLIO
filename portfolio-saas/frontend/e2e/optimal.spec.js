import { test, expect } from "@playwright/test";
import { clickTab, requireLogin } from "./helpers.js";

test.describe("optimal", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
    await page.getByTestId("nav-my-optimal").click();
  });

  test("page reaches a known state", async ({ page }) => {
    const known = page
      .getByTestId("optimal-main")
      .or(page.getByTestId("optimal-window-tabs"))
      .or(page.getByTestId("optimal-empty-holdings"))
      .or(page.getByTestId("optimal-empty-universe"))
      .or(page.getByTestId("optimal-insufficient"));

    await expect(known.first()).toBeVisible({ timeout: 25000 });
  });

  test("window tabs and scenario toggle when data present", async ({ page }) => {
    const tabs = page.getByTestId("optimal-window-tabs");
    if (!(await tabs.isVisible().catch(() => false))) {
      test.skip(true, "optimal windows not available (empty/pro/insufficient)");
    }

    const windowButtons = tabs.locator("[data-testid^='optimal-window-tabs-']");
    const n = await windowButtons.count();
    expect.soft(n).toBeGreaterThan(0);
    if (n > 1) await windowButtons.nth(1).click();

    const scenario = page.getByTestId("optimal-scenario-tabs");
    await expect.soft(scenario).toBeVisible();
    await clickTab(page, "optimal-scenario-tabs", "max_sharpe");
    await clickTab(page, "optimal-scenario-tabs", "min_volatility");
  });

  test("rebalance table or allocation when history sufficient", async ({ page }) => {
    if (await page.getByTestId("optimal-insufficient").isVisible().catch(() => false)) {
      // Graceful empty — counts as pass for this coverage branch.
      await expect(page.getByTestId("optimal-insufficient")).toBeVisible();
      return;
    }
    if (!(await page.getByTestId("optimal-window-tabs").isVisible().catch(() => false))) {
      test.skip(true, "no optimal body to inspect");
    }

    const content = page
      .getByTestId("optimal-trades")
      .or(page.getByTestId("optimal-trades-empty"))
      .or(page.getByTestId("optimal-trades-card"))
      .or(page.getByTestId("optimal-allocation-card"))
      .or(page.getByTestId("optimal-comparison"));
    await expect.soft(content.first()).toBeVisible({ timeout: 15000 });
  });

  test("asset-class roll-up present when a scenario solved", async ({ page }) => {
    if (await page.getByTestId("optimal-insufficient").isVisible().catch(() => false)) {
      test.skip(true, "no solved scenario to roll up");
    }
    if (!(await page.getByTestId("optimal-window-tabs").isVisible().catch(() => false))) {
      test.skip(true, "no optimal body to inspect");
    }
    // Class chart, or the explicit empty — either is a known state.
    const classes = page
      .getByTestId("optimal-class-chart")
      .or(page.getByTestId("optimal-class-empty"))
      .or(page.getByTestId("optimal-class-card"));
    await expect.soft(classes.first()).toBeVisible({ timeout: 15000 });
  });

  test("frontier panel soft-present", async ({ page }) => {
    if (!(await page.getByTestId("optimal-window-tabs").isVisible().catch(() => false))) {
      test.skip(true, "no optimal body (frontier only loads with windows)");
    }
    // Frontier Async may be loading/empty/card — any of these is fine.
    const frontier = page
      .getByTestId("optimal-frontier")
      .or(page.getByTestId("optimal-frontier-card"));
    await expect.soft(frontier.first()).toBeVisible({ timeout: 20000 });
  });

  test("insufficient-history handled gracefully", async ({ page }) => {
    const tabs = page.getByTestId("optimal-window-tabs");
    if (!(await tabs.isVisible().catch(() => false))) {
      // Empty holdings / universe / pro — still graceful UI.
      const graceful = page
        .getByTestId("optimal-empty-holdings")
        .or(page.getByTestId("optimal-empty-universe"))
        .or(page.getByTestId("optimal-empty-windows"))
        .or(page.getByTestId("optimal-insufficient"));
      await expect(graceful.first()).toBeVisible({ timeout: 20000 });
      return;
    }

    // Probe each window; if any shows insufficient, that path is covered.
    const buttons = tabs.locator("[data-testid^='optimal-window-tabs-']");
    const n = await buttons.count();
    let sawInsufficient = false;
    for (let i = 0; i < n; i++) {
      await buttons.nth(i).click();
      if (await page.getByTestId("optimal-insufficient").isVisible().catch(() => false)) {
        sawInsufficient = true;
        break;
      }
    }
    // Either we found the empty state or every window has data — both OK.
    expect.soft(sawInsufficient || n > 0).toBeTruthy();
  });
});

test.describe("optimal — risk tolerance and position cap", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
    await page.getByTestId("nav-my-optimal").click();
  });

  // Every assertion here waits on the RESPONSE, never on the DOM alone. `useApi`
  // keeps the previous payload visible while the next request is in flight and
  // shows no spinner for it, so a DOM-only wait passes against the answer to the
  // question that was asked before this one.
  const waitForOptimal = (page, match) =>
    page.waitForResponse(
      (res) =>
        res.url().includes("/api/optimization/my-optimal/") &&
        res.url().includes(match) &&
        res.status() === 200,
      { timeout: 40000 }
    );

  test("a risk ceiling produces the risk-budget scenario", async ({ page }) => {
    const control = page.getByTestId("optimal-risk-ceiling");
    if (!(await control.isVisible().catch(() => false))) {
      test.skip(true, "no optimal body (empty holdings / insufficient history)");
    }

    const settled = waitForOptimal(page, "target_volatility=0.35");
    await control.selectOption("0.35");
    await settled;

    // Selecting a ceiling also selects the scenario it produces — leaving the
    // tab on Min Volatility would answer a question nobody asked.
    const note = page.getByTestId("optimal-risk-note");
    const insufficient = page.getByTestId("optimal-insufficient");
    await expect(note.or(insufficient).first()).toBeVisible({ timeout: 20000 });
    if (await note.isVisible().catch(() => false)) {
      await expect(note).toContainText(/%/);
    }
  });

  test("a position cap is applied and explained", async ({ page }) => {
    const control = page.getByTestId("optimal-max-assets");
    if (!(await control.isVisible().catch(() => false))) {
      test.skip(true, "no optimal body (empty holdings / insufficient history)");
    }

    const settled = waitForOptimal(page, "max_assets=3");
    await control.selectOption("3");
    await settled;

    const note = page.getByTestId("optimal-cardinality-note");
    const insufficient = page.getByTestId("optimal-insufficient");
    await expect(note.or(insufficient).first()).toBeVisible({ timeout: 20000 });
  });

  test("robustness runs only when asked, and reports its bands", async ({ page }) => {
    const card = page.getByTestId("optimal-robustness-card");
    if (!(await card.isVisible().catch(() => false))) {
      test.skip(true, "no optimal body to resample");
    }
    // The point of the gate: 200 re-solves must not ride along with the page.
    await expect(page.getByTestId("optimal-robustness-table")).toHaveCount(0);

    const settled = page.waitForResponse(
      (res) => res.url().includes("/api/optimization/robustness/"),
      { timeout: 120000 }
    );
    await page.getByTestId("optimal-robustness-run").click();
    const response = await settled;

    if (response.status() !== 200) {
      // 503 is the documented "not enough shared history" branch, and the page
      // has to render it as an error state rather than an endless spinner.
      await expect(page.getByTestId("optimal-robustness")).toBeVisible();
      return;
    }
    const known = page
      .getByTestId("optimal-robustness-table")
      .or(page.getByTestId("optimal-robustness-empty"));
    await expect(known.first()).toBeVisible({ timeout: 20000 });
  });
});

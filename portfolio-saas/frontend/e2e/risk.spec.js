import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

// The four risk views used to sit behind tabs, so three of them were never seen.
// They are stacked now: the contract these tests hold is that all four are on
// screen at once, without anything to click first.
test.describe("risk", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  test("all four risk views render together, with no chart tabs", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }

    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-risk")).toBeVisible({ timeout: 20000 });

    // Not `dashboard-risk-body`: `Async` renders that testId only on its
    // loading/empty/error branches, so asserting it is really asserting that the
    // analytics call did NOT succeed. The panels below are the real contract.

    // The chart-type selector is gone for good.
    await expect(page.getByTestId("dashboard-risk-view")).toHaveCount(0);

    // Each panel shows either its chart or an honest empty state — which one
    // depends on how much history the demo account has.
    const panels = [
      ["risk-money-vs-risk-class", "dashboard-risk-class-empty"],
      ["risk-money-vs-risk", "dashboard-risk-asset-empty"],
      ["risk-correlation", "dashboard-risk-correlation-empty"],
      ["risk-diversifier-scatter", "dashboard-risk-add-empty"],
    ];
    for (const [chart, fallback] of panels) {
      await expect(
        page.getByTestId(chart).or(page.getByTestId(fallback)).first()
      ).toBeVisible({ timeout: 20000 });
    }
  });

  test("risk window control remains available", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }

    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-risk-window")).toBeVisible({ timeout: 20000 });
    await page.getByTestId("dashboard-risk-window-90").click();
    await expect(page.getByTestId("dashboard-risk-window-90")).toHaveAttribute("aria-pressed", "true");
    await page.getByTestId("dashboard-risk-window-365").click();
    await expect(page.getByTestId("dashboard-risk-window-365")).toHaveAttribute("aria-pressed", "true");
  });

  test("the card says how many days it measured and how much of the book", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }

    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-risk")).toBeVisible({ timeout: 20000 });

    // The window buttons ask for 90/180/365 days and the panel starts after the
    // last hole in the warehouse, so what was asked for and what was measured
    // routinely differ. Silence there is what let three buttons that cannot
    // change the answer look like three that can.
    const note = page.getByTestId("dashboard-risk-window-note");
    const correlationEmpty = page.getByTestId("dashboard-risk-correlation-empty");
    await expect(note.or(correlationEmpty).first()).toBeVisible({ timeout: 20000 });
    if (await note.count()) {
      await expect.soft(note).toContainText(/\d+ shared trading days/);
    }

    // Coverage only appears when part of the book is unmeasurable, which is the
    // only time it says anything — a fully covered portfolio needs no caveat.
    const coverage = page.getByTestId("dashboard-risk-coverage-note");
    if (await coverage.count()) {
      await expect.soft(coverage).toContainText("holdings");
    }
  });
});

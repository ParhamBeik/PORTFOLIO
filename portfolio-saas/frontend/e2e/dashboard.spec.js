import { test, expect } from "@playwright/test";
import { clickTab, requireLogin } from "./helpers.js";

/**
 * `toBeVisible()` is not enough for a cell in a horizontally scrolling table.
 *
 * The holdings table is 12 columns wide and overflows its card, so a Save or
 * Delete button appended as the LAST column sat hundreds of pixels past the
 * right edge — present, non-empty, "visible" to Playwright, and unreachable to
 * the reader, who concluded the buttons did nothing. This asserts the thing the
 * user actually needs: the control is inside the scrolled viewport, unscrolled.
 */
async function expectWithinScrollView(locator, containerTestId, page) {
  const box = await locator.boundingBox();
  const container = await page.getByTestId(containerTestId).boundingBox();
  expect(box, "control has no box").not.toBeNull();
  expect(box.x).toBeGreaterThanOrEqual(container.x - 1);
  expect(box.x + box.width).toBeLessThanOrEqual(container.x + container.width + 1);
}

test.describe("dashboard", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  test("loads hero / trend / allocation / holdings panels", async ({ page }) => {
    // New accounts land on onboarding — soft-skip rather than fail CI.
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }

    await page.getByTestId("nav-portfolio").click();
    // `.first()`: both testids are present once the hero renders, and an `.or()`
    // that matches two elements is a strict-mode violation, not a pass.
    await expect
      .soft(page.getByTestId("dashboard-hero").or(page.getByTestId("dashboard-total")).first())
      .toBeVisible({ timeout: 20000 });

    await expect.soft(page.getByTestId("dashboard-trend")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-allocation")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-holdings")).toBeVisible();
  });

  test("range tabs switch", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-trend-tabs")).toBeVisible({ timeout: 20000 });

    for (const value of ["30", "90", "365", "all"]) {
      const ok = await clickTab(page, "dashboard-trend-tabs", value);
      expect.soft(ok, `tab ${value}`).toBeTruthy();
    }
  });

  test("benchmark range tabs match nominal windows", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-trend-basis")).toBeVisible({ timeout: 20000 });
    await clickTab(page, "dashboard-trend-basis", "benchmarks");
    await expect(page.getByTestId("dashboard-trend-tabs")).toBeVisible();

    for (const value of ["30", "90", "365", "all"]) {
      const answered = page.waitForResponse(
        (r) => r.url().includes("/api/analytics/benchmarks/") && r.url().includes(`window=${value}`),
        { timeout: 30000 },
      );
      const ok = await clickTab(page, "dashboard-trend-tabs", value);
      expect.soft(ok, `benchmark tab ${value}`).toBeTruthy();
      const response = await answered;
      expect.soft(response.ok(), `benchmark ${value} response`).toBeTruthy();
    }
  });

  test("real basis shows the CPI rate the chart used", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-trend-basis")).toBeVisible({ timeout: 20000 });

    // Wait on the response, not on the DOM. `useApi` keeps the previous data
    // on screen during a refetch and `Async` shows no spinner while it holds
    // any data, so asserting straight after a tab click reads the answer to
    // the PREVIOUS question and passes without proving anything.
    const answered = page.waitForResponse(
      (r) => r.url().includes("/api/snapshots/") && r.url().includes("real_toman"),
      { timeout: 30000 }
    );
    await clickTab(page, "dashboard-trend-basis", "real");
    const payload = await answered.then((r) => r.json().catch(() => null));

    // Two diverging lines and no number is unfalsifiable: a projection running
    // at triple the published pace draws the same picture as a correct one.
    // Either the rate is on screen, or the series honestly refused to compute.
    const note = page.getByTestId("dashboard-trend-real-note");
    const failed = page.getByTestId("dashboard-trend-real-error");
    await expect(note.or(failed).first()).toBeVisible({ timeout: 20000 });
    if (await note.count()) {
      // "%" alone was satisfied by the growth figure the note always prints,
      // so this passed identically whether or not the rate was named -- the
      // exact blindness the test exists to close. Assert the provenance line,
      // which is the only place the annual rate and its source appear.
      const source = page.getByTestId("dashboard-trend-cpi-source");
      await expect.soft(source).toBeVisible();
      await expect.soft(source).toContainText(/%\/year/);
      // And when the server did report the rate it divided by, the sentence
      // must actually quote it rather than quietly dropping the clause.
      if (payload?.cpi?.applied_annual_rate != null) {
        await expect.soft(note).toContainText(/rose .*% a year/);
      }
    }
  });

  test("all-portfolios exposes edit and delete controls from card header", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-portfolio").click();
    const scope = page.getByTestId("scope-account");
    await expect(scope).toBeVisible({ timeout: 15000 });
    await scope.selectOption("");

    await expect.soft(page.getByTestId("dashboard-holdings-manage-edit")).toBeVisible({ timeout: 15000 });
    await expect.soft(page.getByTestId("dashboard-holdings-manage-delete")).toBeVisible();

    await page.getByTestId("dashboard-holdings-manage-edit").click();
    const qtyEdit = page.getByTestId("dashboard-holdings-edit-qty").first();
    if (await qtyEdit.count()) {
      await expect.soft(qtyEdit).toBeVisible();
      const save = page.getByTestId("dashboard-holdings-save").first();
      await expect.soft(save).toBeVisible();
      await expectWithinScrollView(save, "dashboard-holdings", page);
    }
    // Every holding is renamable, not just house and manual rows.
    await expect
      .soft(page.getByTestId("dashboard-holdings-edit-name").first())
      .toBeVisible();
  });

  test("basis switch if present", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    const basis = page.getByTestId("scope-basis");
    await expect(basis).toBeVisible();
    await basis.selectOption("usd_denominated");
    await expect.soft(basis).toHaveValue("usd_denominated");
    await basis.selectOption("nominal_toman");
  });

  test("add opens the step-by-step dialog when a portfolio is selected", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }
    await page.getByTestId("nav-portfolio").click();
    const scope = page.getByTestId("scope-account");
    const options = await scope.locator("option").all();
    // option[0] is "All portfolios"; need a real account id
    if (options.length < 2) {
      test.skip(true, "no account to select for add/edit UI");
    }
    const value = await options[1].getAttribute("value");
    await scope.selectOption(value);

    // The quick-add row was replaced by the same guided dialog the Ledger uses,
    // so there is one add flow instead of two that disagreed.
    const addButton = page.getByTestId("dashboard-add-button");
    await expect(addButton).toBeVisible({ timeout: 15000 });
    await addButton.click();
    await expect(page.getByTestId("add-transaction")).toBeVisible();
    await expect(page.getByTestId("add-transaction-category-gold")).toBeVisible();
    await page.getByTestId("add-transaction-close").click();
    await expect(page.getByTestId("add-transaction")).toHaveCount(0);

    await expect.soft(page.getByTestId("dashboard-holdings-manage-edit")).toBeVisible();
    await page.getByTestId("dashboard-holdings-manage-edit").click();
    const qtyEdit = page.getByTestId("dashboard-holdings-edit-qty").first();
    const priceEdit = page.getByTestId("dashboard-holdings-edit-price").first();
    const saveBtn = page.getByTestId("dashboard-holdings-save").first();
    if (await qtyEdit.count()) {
      await expect.soft(qtyEdit).toBeVisible();
      await expect.soft(saveBtn).toBeVisible();
      await expect.soft(saveBtn).toBeDisabled();
    }
    if (await priceEdit.count()) {
      await expect.soft(priceEdit).toBeVisible();
    }

    // The quantity spinner has to move in whole units for a counted asset. A
    // step of "any" walked stock positions in fractions of a share, and the
    // arrow key is the fastest way to prove which one the box is offering:
    // a counted row goes 12 -> 13, never 12 -> 12.0001.
    if (await qtyEdit.count()) {
      const step = await qtyEdit.getAttribute("step");
      expect.soft(step, "quantity box declares a step").not.toBe("any");
      if (step === "1") {
        // Seeded from the API's Decimal string, so a holding of four coins
        // opened the editor reading "4.000000" beside a column printing "4".
        expect.soft(await qtyEdit.inputValue()).not.toMatch(/\.\d*0$/);
        const before = Number(await qtyEdit.inputValue());
        await qtyEdit.press("ArrowUp");
        const after = Number(await qtyEdit.inputValue());
        expect.soft(after).toBe(before + 1);
        await qtyEdit.press("ArrowDown");
        expect.soft(Number(await qtyEdit.inputValue())).toBe(before);
      }
    }

    await page.getByTestId("dashboard-holdings-manage-delete").click();
    // Deleting is destructive, so the mode says what it does before you click.
    await expect
      .soft(page.getByTestId("dashboard-holdings-delete-hint"))
      .toBeVisible();
    const del = page.getByTestId("dashboard-holdings-delete").first();
    if (await del.count()) {
      await expect.soft(del).toBeVisible();
      await expectWithinScrollView(del, "dashboard-holdings", page);
    }
  });
});

import { test, expect } from "@playwright/test";
import { clickTab, requireLogin } from "./helpers.js";

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
    await expect
      .soft(page.getByTestId("dashboard-hero").or(page.getByTestId("dashboard-total")))
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
      await expect.soft(page.getByTestId("dashboard-holdings-save").first()).toBeVisible();
    }
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

  test("inline add controls when a portfolio is selected", async ({ page }) => {
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

    await expect.soft(page.getByTestId("dashboard-add-holding")).toBeVisible({ timeout: 15000 });
    await expect.soft(page.getByTestId("dashboard-add-asset-select")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-add-quantity")).toBeVisible();
    await expect.soft(page.getByTestId("dashboard-add-button")).toBeVisible();

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
    await page.getByTestId("dashboard-holdings-manage-delete").click();
    const del = page.getByTestId("dashboard-holdings-delete").first();
    if (await del.count()) {
      await expect.soft(del).toBeVisible();
    }
  });
});

import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("risk", () => {
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  test("risk section exposes the visual decision tabs", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }

    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-risk")).toBeVisible({ timeout: 20000 });
    await expect(page.getByTestId("dashboard-risk-body")).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId("dashboard-risk-view")).toBeVisible();

    for (const value of ["class", "asset", "correlation", "add"]) {
      await expect(page.getByTestId(`dashboard-risk-view-${value}`)).toBeVisible();
    }
  });

  test("switching visual tabs renders the corresponding chart state", async ({ page }) => {
    if (await page.getByTestId("onboarding-card").isVisible().catch(() => false)) {
      test.skip(true, "account has no holdings (onboarding)");
    }

    await page.getByTestId("nav-portfolio").click();
    await expect(page.getByTestId("dashboard-risk-body")).toBeVisible({ timeout: 20000 });

    await expect(
      page.getByTestId("risk-money-vs-risk-class").or(page.getByTestId("dashboard-risk-class-empty"))
    ).toBeVisible({ timeout: 15000 });

    await page.getByTestId("dashboard-risk-view-asset").click();
    await expect(
      page.getByTestId("risk-money-vs-risk").or(page.getByTestId("dashboard-risk-asset-empty"))
    ).toBeVisible({ timeout: 15000 });

    await page.getByTestId("dashboard-risk-view-correlation").click();
    await expect(
      page.getByTestId("risk-correlation").or(page.getByTestId("dashboard-risk-correlation-empty"))
    ).toBeVisible({ timeout: 15000 });

    await page.getByTestId("dashboard-risk-view-add").click();
    await expect(
      page.getByTestId("risk-diversifier-scatter").or(page.getByTestId("dashboard-risk-add-body"))
    ).toBeVisible({ timeout: 15000 });
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
});

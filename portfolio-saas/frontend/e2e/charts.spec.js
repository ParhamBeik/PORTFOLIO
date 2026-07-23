import { expect, test } from "@playwright/test";

const PASSWORD = "Sup3rSecret!";
const FAMILY_PASSWORD = "family12345";

async function login(page, email, password = PASSWORD) {
  await page.goto("/login");
  await page.getByLabel("Email Address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In" }).click();
  await expect(page.getByRole("link", { name: "Portfolio", exact: true })).toBeVisible();
}

test.describe("Chart Suite, Dark Mode, & Portfolio Insights", () => {
  test("Theme toggle switches between dark and light mode seamlessly", async ({ page }) => {
    await login(page, "family@portfolio.local", FAMILY_PASSWORD);

    const toggleBtn = page.getByRole("button", { name: "Toggle theme mode" });
    await expect(toggleBtn).toBeVisible();

    // Toggle theme to light
    await toggleBtn.click();
    const themeAttr1 = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
    expect(themeAttr1).toBe("light");

    // Toggle theme back to dark
    await toggleBtn.click();
    const themeAttr2 = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
    expect(themeAttr2).toBe("dark");
  });

  test("NetWorthChart renders cleanly and handles timeframe + currency toggles", async ({ page }) => {
    await login(page, "family@portfolio.local", FAMILY_PASSWORD);

    await expect(page.locator(".chart-container")).toBeVisible();
    await expect(page.locator(".recharts-responsive-container")).toBeVisible();

    // Timeframe buttons (7D, 30D, 90D, ALL)
    for (const timeframe of ["7D", "30D", "90D", "ALL"]) {
      await page.getByRole("button", { name: timeframe, exact: true }).click();
      await expect(page.locator(".recharts-responsive-container")).toBeVisible();
    }

    // Currency buttons (IRT / USD)
    await page.getByRole("button", { name: "USD ($)" }).click();
    await page.getByRole("button", { name: "IRT (TMN)" }).click();
  });

  test("Portfolio Breakdown & Asset Allocation section displays below chart", async ({ page }) => {
    await login(page, "family@portfolio.local", FAMILY_PASSWORD);

    // Verify Asset Allocation & Portfolio Insights section exists below chart
    await expect(page.locator(".allocation-insights-container")).toBeVisible();
    await expect(page.getByText("Portfolio Allocation & Insights Breakdown")).toBeVisible();
    await expect(page.getByText("Asset Class Allocation")).toBeVisible();
  });

  test("Market Explorer performance chart renders SVG elements", async ({ page }) => {
    await login(page, "family@portfolio.local", FAMILY_PASSWORD);

    await page.getByRole("link", { name: "Market" }).click();
    await expect(page.getByRole("heading", { name: "Market Explorer" })).toBeVisible();

    // Select Gold category
    await page.getByRole("button", { name: "🥇 Gold" }).click();

    // Wait for performance chart section and SVG surface to load
    await expect(page.locator(".market-chart .recharts-surface")).toBeVisible({ timeout: 10000 });
  });
});

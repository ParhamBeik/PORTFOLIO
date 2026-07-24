import { expect, test } from "@playwright/test";

test.use({ extraHTTPHeaders: { "X-Forwarded-For": "198.51.100.50" } });

const PASSWORD = "Sup3rSecret!";

async function login(page, email = "e2e-free@portfolio.local") {
  await page.goto("/login");
  await page.getByLabel("Email Address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign In" }).click();
  await expect(page.getByRole("link", { name: "Portfolio", exact: true })).toBeVisible();
}

test("authenticated pages fit a mobile viewport without document overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);

  for (const link of ["Market", "Optimization", "Billing", "Profile", "Portfolio"]) {
    await page.getByRole("link", { name: link, exact: true }).click();
    await expect(page.locator("main")).toBeVisible();
    const dimensions = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      content: document.documentElement.scrollWidth,
    }));
    expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
  }
});

test("keyboard users can skip navigation and reach primary destinations", async ({ page }) => {
  await login(page);
  await expect(page.getByRole("img", { name: /Net worth history/ })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  await page.getByRole("link", { name: "Market", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Market Explorer" })).toBeVisible();
});

test("expired sessions redirect to sign in during an active workflow", async ({ page }) => {
  await login(page);
  await page.route("**/api/auth/me/**", (route) =>
    route.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"expired"}' })
  );
  await page.route("**/api/token/refresh/**", (route) =>
    route.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"expired"}' })
  );

  await page.reload();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("button", { name: "Sign In" })).toBeVisible();
});

test("temporary account-loading failures preserve the session and allow retry", async ({ page }) => {
  await login(page);
  let failAccountLoad = true;
  await page.route("**/api/auth/me/**", (route) => {
    if (failAccountLoad) {
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: '{"detail":"Service temporarily unavailable"}',
      });
    }
    return route.continue();
  });

  await page.reload();
  await expect(page.getByRole("alert")).toContainText("Service temporarily unavailable");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("ps_refresh"))).not.toBeNull();

  failAccountLoad = false;
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("link", { name: "Portfolio", exact: true })).toBeVisible();
});

test("market page keeps its navigation context while the catalog loads", async ({ page }) => {
  await login(page);
  await page.route("**/api/market/assets/**", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue();
  });

  await page.goto("/market");
  await expect(page.getByRole("heading", { name: "Market Explorer" })).toBeVisible();
  await expect(page.getByText("Loading market data…")).toBeVisible();
});

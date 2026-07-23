import { expect, test } from "@playwright/test";

const PASSWORD = "Sup3rSecret!";

async function login(page, email) {
  await page.goto("/login");
  await page.getByLabel("Email Address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign In" }).click();
  await expect(page.getByRole("link", { name: "Portfolio" })).toBeVisible();
}

test("registration validates credentials and creates a session", async ({ page }) => {
  await page.goto("/register");

  await page.getByLabel("Email Address").fill("invalid");
  await page.getByLabel("Password", { exact: true }).fill("123");
  await expect(page.getByText("Please enter a valid email address")).toBeVisible();
  await expect(page.getByText("At least 8 characters")).toBeVisible();
  await expect(page.getByRole("button", { name: "Create Account" })).toBeDisabled();

  await page.getByRole("button", { name: "Show password" }).click();
  await expect(page.getByLabel("Password", { exact: true })).toHaveAttribute("type", "text");
  await page.getByRole("button", { name: "Hide password" }).click();

  await page.getByLabel("First Name").fill("Test");
  await page.getByLabel("Email Address").fill(`e2e-new-${Date.now()}@portfolio.local`);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Confirm Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create Account" }).click();
  await expect(page.getByRole("link", { name: "Portfolio" })).toBeVisible();
});

test("free user can manage a portfolio, holding, trades, and chart controls", async ({ page }) => {
  await login(page, "e2e-free@portfolio.local");

  const portfolioName = `E2E ${Date.now()}`;
  const renamedPortfolio = `${portfolioName} edited`;
  await page.getByLabel("New portfolio name").fill(portfolioName);
  await page.getByRole("button", { name: "Create portfolio" }).click();
  await page.getByRole("button", { name: "Open →" }).last().click();
  await expect(page.locator("strong", { hasText: portfolioName })).toBeVisible();

  await page.getByTitle("Edit portfolio").click();
  await page.getByLabel("Portfolio name").fill(renamedPortfolio);
  await page.getByLabel("Broker").fill("E2E Broker");
  await page.getByLabel("Portfolio goal").fill("Retirement");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.locator("strong", { hasText: renamedPortfolio })).toBeVisible();
  await expect(page.getByText("E2E Broker", { exact: false })).toBeVisible();
  await expect(page.getByText("Retirement", { exact: true })).toBeVisible();

  await page.getByLabel("Asset to add").selectOption("house_asset");
  await page.getByLabel("House price per square meter").fill("90");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByRole("cell", { name: "90", exact: true })).toBeVisible();

  await page.getByTitle("Edit quantity").click();
  await page.getByRole("cell", { name: "✓ ✕" }).getByLabel("House price per square meter").fill("95");
  await page.getByRole("button", { name: "✓" }).click();
  await expect(page.getByRole("cell", { name: "95", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "8,169,000,000", exact: true })).toBeVisible();

  for (const range of ["30D", "90D", "All", "7D"]) {
    await page.getByRole("button", { name: range, exact: true }).click();
  }

  await page.getByLabel("Trade side").selectOption("buy");
  await page.getByLabel("Trade asset").selectOption("emami_coin");
  await page.getByLabel("Trade quantity").fill("2");
  await page.getByRole("button", { name: "Execute" }).click();
  await expect(page.getByRole("status")).toContainText("Bought");

  await page.getByLabel("Trade side").selectOption("sell");
  await page.getByLabel("Trade quantity").fill("3");
  await page.getByRole("button", { name: "Execute" }).click();
  await expect(page.getByRole("status")).toContainText("Cannot sell 3; only 2 is held");

  await page.getByLabel("Trade quantity").fill("1");
  await page.getByRole("button", { name: "Execute" }).click();
  await expect(page.getByRole("status")).toContainText("Sold");
  await expect(page.getByRole("button", { name: /Undo sell/ })).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: /Undo sell/ }).click();
  await expect(page.getByRole("status")).toContainText("Trade undone");
  await expect(page.getByRole("button", { name: /Undo buy/ })).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: /Undo buy/ }).click();
  await expect(page.getByRole("status")).toContainText("Trade undone");

  await page.getByRole("button", { name: "USD ($)" }).click();
  await page.getByRole("button", { name: "Tomans (TMN)" }).click();

  await page.getByRole("button", { name: "Remove Real Estate" }).click();
  await expect(page.getByRole("row", { name: /Real Estate/ })).toHaveCount(0);

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByTitle("Delete portfolio").click();
  await expect(page.getByText(renamedPortfolio, { exact: true })).toHaveCount(0);
});

test("free user can browse markets and sees Pro gates", async ({ page }) => {
  await login(page, "e2e-free@portfolio.local");
  let catalogRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/market/assets/") catalogRequests += 1;
  });

  await page.getByRole("link", { name: "Market" }).click();
  await expect(page.getByRole("heading", { name: "Market Explorer" })).toBeVisible();
  await expect.poll(() => catalogRequests).toBeGreaterThan(0);
  const initialCatalogRequests = catalogRequests;
  await page.getByRole("button", { name: "3Y" }).click();
  await page.getByRole("button", { name: "All", exact: true }).click();
  await page.getByRole("button", { name: "Gold" }).click();
  await expect(page.getByLabel("Select Asset")).not.toHaveValue("");
  const peerCard = page.locator(".comparison-card").first();
  if (await peerCard.count()) {
    await peerCard.focus();
    await peerCard.press("Enter");
  }
  await page.getByRole("button", { name: "Stocks" }).click();
  await expect(page.getByLabel("Sector / Industry")).toBeVisible();
  await page.getByLabel("Search Symbol").fill("KAMA");
  await expect(page.getByLabel("Select Asset")).not.toHaveValue("");
  expect(catalogRequests).toBe(initialCatalogRequests);

  await page.getByRole("link", { name: "Optimization" }).click();
  await expect(page.getByRole("heading", { name: "This is a Pro feature" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Upgrade to Pro" })).toBeVisible();

  await page.getByRole("link", { name: "Billing" }).click();
  await expect(page.getByText("Free", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Upgrade to Pro" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Admin" })).toHaveCount(0);
});

test("Pro user can use optimizer, insights, analytics, and sees expiry", async ({ page }) => {
  await login(page, "e2e-pro@portfolio.local");

  await page.getByRole("link", { name: "Optimization" }).click();
  await expect(page.getByRole("heading", { name: "Portfolio Optimization" })).toBeVisible();
  for (const scenario of ["Min Volatility", "Risk Parity", "HRP", "Max Sharpe"]) {
    await page.getByRole("button", { name: scenario }).click();
    await expect(page.getByRole("heading", { name: "Current vs target allocation" })).toBeVisible();
  }
  await expect(page.getByRole("heading", { name: "Rebalance trades" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Efficient frontier" })).toBeVisible();
  await page.getByRole("link", { name: "Advanced Insights" }).click();
  await expect(page.getByRole("heading", { name: "Advanced Insights" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Allocation by asset class" })).toBeVisible();
  await page.getByRole("link", { name: "Risk Analytics" }).click();
  await expect(page.getByRole("heading", { name: "Portfolio Analytics" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Risk & return" })).toBeVisible();
  await expect(page.locator(".error")).toHaveCount(0);

  await page.getByRole("link", { name: "Market" }).click();
  await page.getByRole("button", { name: "Stocks" }).click();
  await expect(page.getByRole("heading", { name: "Codal Announcements" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Major Shareholders" })).toBeVisible();

  await page.getByRole("link", { name: "Billing" }).click();
  await expect(page.getByText("Pro", { exact: true })).toBeVisible();
  await expect(page.getByText(/until/)).toBeVisible();

  await page.goto("/billing?status=success&ref_id=E2E-REF");
  await expect(page.getByText(/Pro subscription is active/)).toBeVisible();
  await expect(page.getByText(/E2E-REF/)).toBeVisible();
});

test("staff user can open and filter the admin portal", async ({ page }) => {
  await login(page, "e2e-admin@portfolio.local");
  await page.getByRole("link", { name: "Admin" }).click();
  await expect(page.getByRole("heading", { name: /System Diagnostics/ })).toBeVisible();
  await page.getByRole("button", { name: /Refresh Status/ }).click();
  await page.getByRole("button", { name: /Rate Limits/ }).click();
  await expect(page.getByRole("heading", { name: "TSETMC & Market Provider Quotas" })).toBeVisible();
  await page.getByRole("button", { name: /User & Portfolio Health/ }).click();
  await expect(page.getByRole("heading", { name: "User & Portfolio Analytics" })).toBeVisible();
  await page.getByRole("button", { name: /Database & Storage Stats/ }).click();
  await expect(page.getByRole("heading", { name: "Database Storage & Row Counts" })).toBeVisible();
  await page.getByRole("button", { name: /Asset Archive/ }).click();
  await page.getByLabel("Search archive gaps").fill("stock");
  await page.getByLabel("Filter archive gaps by endpoint").selectOption("stock_history_unadjusted");
  await page.getByLabel("Filter archive gaps by status").selectOption("PENDING");
});

test("logout clears the session and protects deep links", async ({ page }) => {
  await login(page, "e2e-free@portfolio.local");
  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/$/);
  await page.goto("/accounts/999999");
  await expect(page).toHaveURL(/\/$/);
  await page.goto("/insights");
  await expect(page).toHaveURL(/\/optimization\/insights$/);
  await page.goto("/analytics");
  await expect(page).toHaveURL(/\/optimization\/analytics$/);
  await page.goto("/billing?status=cancel");
  await expect(page.getByText(/Payment was cancelled/)).toBeVisible();
  await page.goto("/billing?status=error");
  await expect(page.getByText(/could not verify the payment/)).toBeVisible();
  await page.getByRole("button", { name: "Logout" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/market");
  await expect(page).toHaveURL(/\/login$/);
});

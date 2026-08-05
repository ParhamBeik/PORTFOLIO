import { expect, test } from "@playwright/test";
import { execSync } from "child_process";

test.use({ extraHTTPHeaders: { "X-Forwarded-For": "198.51.100.10" } });

test.beforeAll(async () => {
  try {
    execSync(`docker compose exec -T backend python manage.py shell -c "
from accounts.models import User
from portfolio.models import Account, Holding, LedgerEntry
from django.utils import timezone
# Clean up demopro user accounts to baseline
u_pro = User.objects.filter(email='demopro@portfolio.local').first()
if u_pro:
    u_pro.tier = User.Tier.PRO
    u_pro.pro_expires_at = timezone.now() + timezone.timedelta(days=365)
    u_pro.save(update_fields=['tier', 'pro_expires_at'])
    stale = Account.objects.filter(user=u_pro).exclude(name='Main Portfolio')
    for acct in stale:
        LedgerEntry.objects.filter(account=acct, reversal_of__isnull=False).update(reversal_of=None)
        LedgerEntry.objects.filter(account=acct).delete()
        Holding.objects.filter(account=acct).delete()
        acct.delete()
# Delete registered users
User.objects.filter(email__startswith='e2e-new-').delete()
"`, { stdio: 'inherit' });
  } catch (err) {
    console.error("Database cleanup failed:", err);
  }
});

test("user can manage a portfolio, holding, trades, and chart controls", async ({ page }) => {
  await page.goto("/");

  const portfolioName = `E2E ${Date.now()}`;
  const renamedPortfolio = `${portfolioName} edited`;
  await page.getByLabel("New portfolio name").fill(portfolioName);
  await page.getByRole("button", { name: "Create portfolio" }).click();
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
  await expect(page.getByRole("cell", { name: "8,569,000,000", exact: true })).toBeVisible();

  for (const range of ["30D", "90D", "ALL", "7D"]) {
    await page.getByRole("button", { name: range, exact: true }).click();
  }

  // Provision newly created portfolio with cash to prevent Insufficient cash balance error
  execSync(`docker compose exec -T backend python manage.py shell -c "
from accounts.models import User
from portfolio.models import Account
u = User.objects.get(email='demopro@portfolio.local')
a = Account.objects.get(user=u, name='${renamedPortfolio}')
a.cash_balance_tomans = 1000000000
a.save()
"`);

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

  await page.getByRole("button", { name: /USD/ }).click();
  await page.getByRole("button", { name: /IRT/ }).click();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Remove Real Estate" }).click();
  await expect(page.locator("#holdings-table").getByRole("row", { name: /Real Estate/ })).toHaveCount(0);

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByTitle("Delete portfolio").click();
  await expect(page.getByText(renamedPortfolio, { exact: true })).toHaveCount(0);
});

test("user can browse markets and view charts", async ({ page }) => {
  await page.goto("/");
  let catalogRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/market/assets/") catalogRequests += 1;
  });

  await page.getByRole("link", { name: "Market" }).click();
  await expect(page.getByRole("heading", { name: "Market Explorer" })).toBeVisible();
  await expect.poll(() => catalogRequests).toBeGreaterThan(0);
  await expect(page.getByText("Loading market data")).toHaveCount(0, { timeout: 60000 });
  const initialCatalogRequests = catalogRequests;
  await page.getByRole("button", { name: "3Y" }).click();
  await page.getByRole("button", { name: "All", exact: true }).click();
  await page.getByRole("button", { name: /Gold/ }).click();
  await expect(page.getByLabel("Select Asset")).not.toHaveValue("");
  const peerCard = page.locator(".comparison-card").first();
  if (await peerCard.count()) {
    await peerCard.focus();
    await peerCard.press("Enter");
  }
  await page.getByRole("button", { name: /Stocks/ }).click();
  await expect(page.getByLabel("Sector / Industry")).toBeVisible();
  await page.getByLabel("Search Symbol").fill("KAMA");
  await expect(page.getByLabel("Select Asset")).not.toHaveValue("");
  expect(catalogRequests).toBe(initialCatalogRequests);
});

test("user can use optimizer, insights, and analytics", async ({ page }) => {
  await page.goto("/");

  const portfolioSelect = page.locator("#portfolio-scope");
  const accountValue = await portfolioSelect.locator("option:not([value=''])").first().getAttribute("value");
  await portfolioSelect.selectOption(accountValue);

  const scopedOptimization = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/optimization/" && url.searchParams.get("account") === accountValue;
  });
  await page.getByRole("link", { name: "Optimization" }).click();
  await scopedOptimization;
  await expect(page.getByRole("heading", { name: "Portfolio Optimization" })).toBeVisible();
  for (const scenario of ["Min Volatility", "Risk Parity", "HRP", "Max Sharpe"]) {
    await page.getByRole("button", { name: scenario }).click();
    await expect(page.getByRole("heading", { name: "Current vs target allocation" })).toBeVisible({ timeout: 30000 });
  }
  await expect(page.getByRole("heading", { name: "Rebalance trades" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Efficient frontier" })).toBeVisible({ timeout: 60000 });
  const scopedInsights = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/insights/" && url.searchParams.get("account") === accountValue;
  });
  await page.getByRole("link", { name: "Advanced Insights" }).click();
  await scopedInsights;
  await expect(page.getByRole("heading", { name: "Advanced Insights" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Allocation by asset class" })).toBeVisible();
  const scopedAnalytics = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/analytics/" && url.searchParams.get("account") === accountValue;
  });
  await page.getByRole("link", { name: "Risk Analytics" }).click();
  await scopedAnalytics;
  await expect(page.getByRole("heading", { name: "Portfolio Analytics" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Risk & return" })).toBeVisible();
  await expect(page.locator(".error")).toHaveCount(0);

  await page.getByRole("link", { name: "Market" }).click();
  await page.getByRole("button", { name: /Stocks/ }).click();
  await expect(page.getByRole("heading", { name: "Codal Announcements" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Major Shareholders" })).toBeVisible();
});

test("staff user can open and filter the admin portal", async ({ page }) => {
  execSync(`docker compose exec -T backend python manage.py shell -c "
from accounts.models import User
u = User.objects.get(email='demopro@portfolio.local')
u.is_staff = True
u.save()
"`);

  try {
    await page.goto("/");
    await page.getByRole("link", { name: "Admin", exact: true }).click();
    await expect(page.getByRole("heading", { name: /Real-Time System Operations/ })).toBeVisible();
    await page.getByRole("button", { name: /Refresh/ }).click();
    await expect(page.getByRole("heading", { name: /13 BrsApi Endpoint Families/ })).toBeVisible();
    await page.getByLabel("Search log console").fill("fetch");
    await page.getByLabel("Filter log level").selectOption("WARNING");
    await page.getByLabel("Filter log category").selectOption("FETCH_ERROR");
    await page.getByRole("button", { name: /Backfill Jobs/ }).click();
    await expect(page.getByRole("heading", { name: /Archive Backfill Jobs/ })).toBeVisible();
  } finally {
    execSync(`docker compose exec -T backend python manage.py shell -c "
from accounts.models import User
u = User.objects.get(email='demopro@portfolio.local')
u.is_staff = False
u.save()
"`);
  }
});

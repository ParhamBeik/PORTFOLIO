import { expect, test } from "@playwright/test";

const profile = { id: 1, email: "fixture@example.test", role: "admin", risk_profile: "balanced" };
const accounts = [{ id: 7, name: "Family book", holdings: [{ id: 1, asset_key: "fixture", quantity: "3" }] }];
const valuation = {
  total: "300000", basis: "nominal_toman", total_assets: 1, priced_assets: 1,
  items: [{ key: "fixture", label: "سهام", class: "Stocks", quantity: "3", quantity_step: "1", value: "300000", unit_price: "100000", unit_price_currency: "rial" }],
  accounts: [{ id: 7, name: "Family book", total: "300000", items: [] }],
};

async function fixtureApi(page, role = "admin") {
  await page.addInitScript(() => localStorage.setItem("lattice_session", "1"));
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let body;
    if (path === "/api/token/refresh/") body = { access: "fixture-access" };
    else if (path === "/api/auth/me/") body = { ...profile, role };
    else if (path === "/api/accounts/") body = accounts;
    else if (path === "/api/valuation/") body = valuation;
    else if (path === "/api/snapshots/") body = { basis: "nominal_toman", series: [{ date: "2026-09-23", total: "300000" }] };
    else if (path === "/api/auth/registration/") body = { registration_open: true, self_service_reset: false };
    else return route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"Fixture unavailable"}' });
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}

test("ordinary users are redirected away from Operations and legacy routes resolve", async ({ page }) => {
  await fixtureApi(page, "user");
  await page.goto("/ops");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("nav-operations")).toHaveCount(0);
  await page.goto("/ledger");
  await expect(page).toHaveURL(/\/activity$/);
  await page.goto("/comparison");
  await expect(page).toHaveURL(/\/compare$/);
});

for (const width of [390, 768, 1366]) {
  test(`MVP shell fits ${width}px and keeps destinations reachable`, async ({ page }) => {
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.setViewportSize({ width, height: 850 });
    await fixtureApi(page);
    for (const path of ["/", "/activity", "/research", "/compare", "/risk", "/ops", "/onboarding"]) {
      await page.goto(path);
      // The scope controls are the header's one fixture at every width; the
      // brand steps aside on a phone, where the Home tab is the way home.
      await expect(page.getByTestId("header-toolbar")).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `${path} overflows at ${width}px`).toBeLessThanOrEqual(1);
    }
    // The inline rail starts at xl (1280px); below it the links are in the drawer.
    if (width < 1280) {
      await page.getByTestId("nav-toggle").click();
      await expect(page.getByTestId("nav-drawer")).toBeVisible();
      await expect(page.getByTestId("nav-operations-mobile")).toBeVisible();
    } else {
      await expect(page.getByTestId("nav-operations")).toBeVisible();
    }
    await page.goto("/ops");
    await page.getByTestId("ops-tabs-asset").click();
    if (width < 640) await expect(page.getByLabel("Sort assets")).toBeVisible();
    expect(pageErrors).toEqual([]);
  });
}

import { expect, test } from "@playwright/test";

/**
 * Layout guard: the bugs a phone finds and a desktop review does not.
 *
 * Every check here is one that shipped broken and was found by hand:
 *   - a page wider than the phone (a tab row in a card header stretched the
 *     whole page sideways to 631px on a 390px screen);
 *   - a chart tooltip placed off the screen edge (tapping the allocation ring
 *     put it 22px past the left edge, the asset name cut off);
 *   - English left in the Persian UI, which the bidi algorithm then reorders
 *     ("9 of 10 prices aren't live" printed as "of 10 prices ... 9").
 *
 * The API is a fixture, so this runs without a backend or credentials and
 * fails only for the layout, never for the data.
 */

const DAY = 86400000;
const today = Date.UTC(2026, 9, 5);
const iso = (t) => new Date(t).toISOString().slice(0, 10);

const accounts = [
  { id: 7, name: "اصلی", goal: "", holdings: [{ id: 1, asset_key: "emami_coin", quantity: "3", label: "سکه امامی" }] },
  // A long name: it used to widen the header picker and double every phone card.
  { id: 8, name: "پس‌انداز بلندمدت بچه‌ها برای دانشگاه و خرید خانه در آینده", goal: "", holdings: [{ id: 2, asset_key: "kama_stock", quantity: "250000", label: "کاما" }] },
];

const items = [
  { key: "emami_coin", label: "سکه امامی", class: "Gold", quantity: "3", quantity_step: "1", unit_price: "176000000", unit_price_currency: "toman", value: "528000000", quality_status: "live", account_id: 7, account_name: accounts[0].name, priced_at: new Date(today).toISOString(), age_seconds: 120 },
  { key: "kama_stock", label: "کاما", class: "Stock", quantity: "250000", quantity_step: "1", unit_price: "1780", unit_price_currency: "rial", value: "44500000", quality_status: "stale", account_id: 8, account_name: accounts[1].name, priced_at: new Date(today).toISOString(), age_seconds: 9000 },
];

const valuation = {
  total: "1572500000", cash_tomans: "1000000000", basis: "nominal_toman",
  total_assets: 2, priced_assets: 2, quality_status: "partial", excluded: [], liabilities: [], total_liabilities: 0,
  items, hidden_items: [],
  markets: [
    { market: "tse", label: "Stocks", open: false, last_priced_at: new Date(today).toISOString() },
    { market: "gold_currency", label: "Gold & FX", open: true, last_priced_at: new Date(today).toISOString() },
  ],
  accounts: accounts.map((a, i) => ({
    id: a.id, name: a.name, total: i ? "44500000" : "1528000000", cash_tomans: i ? "0" : "1000000000",
    items: items.filter((it) => it.account_id === a.id), hidden_items: [], liabilities: [], total_liabilities: 0,
  })),
};

// Thirty days that move, so the chart has a line to hover and ticks to label.
const series = Array.from({ length: 30 }, (_, i) => ({
  date: iso(today - (29 - i) * DAY),
  total: String(1500000000 + Math.round(Math.sin(i / 4) * 40000000) + i * 2000000),
  is_estimated: i < 20,
}));

function respond(path) {
  if (path === "/api/token/refresh/") return { access: "fixture-access" };
  if (path === "/api/auth/me/") return { id: 1, email: "fixture@example.test", role: "user", risk_profile: "balanced", date_joined: "2026-01-01T00:00:00Z" };
  if (path === "/api/auth/csrf/") return {};
  if (path === "/api/accounts/") return accounts;
  if (path === "/api/valuation/") return valuation;
  if (path === "/api/snapshots/") return { basis: "nominal_toman", series, trades: [] };
  if (/^\/api\/accounts\/\d+\/liabilities\/$/.test(path)) return [];
  if (/^\/api\/accounts\/\d+\/performance\/$/.test(path)) {
    return { performance_available: false, reason: "opening_baseline_missing", detail: "Complete an opening baseline before calculating performance." };
  }
  if (path === "/api/corporate-actions/") return { results: [] };
  if (path === "/api/ledger/") return [];
  if (path === "/api/assets/") {
    return [{ id: 1, key: "emami_coin", name: "Emami Coin", name_fa: "سکه امامی", asset_class: "Gold", is_manual: false, is_house: false, is_active: true, tse_symbol: "", quantity_step: "1" }];
  }
  if (path === "/api/assets/catalog/") {
    return [{ key: "emami_coin", name: "Emami Coin", name_fa: "سکه امامی", asset_class: "Gold", is_manual: false, source: "gold", symbol: "IR_COIN_EMAMI" }];
  }
  if (path === "/api/guidance/") return { risk_profile: "balanced", personal: null, benchmark: null, basis: "real_toman" };
  return null;
}

async function fixtureApi(page, { lang } = {}) {
  await page.addInitScript((l) => {
    localStorage.setItem("lattice_session", "1");
    if (l) localStorage.setItem("lang", l);
  }, lang || "");
  await page.route("**/api/**", (route) => {
    const body = respond(new URL(route.request().url()).pathname);
    if (body === null) {
      return route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"Fixture unavailable"}' });
    }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}

/** Elements that reach past the viewport, outside any element that scrolls on purpose. */
function overflow(page, width) {
  return page.evaluate((vw) => {
    const clipped = (el) => {
      for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
        if (getComputedStyle(a).overflowX !== "visible") return true;
      }
      return false;
    };
    const out = [];
    for (const el of document.querySelectorAll("body *")) {
      const r = el.getBoundingClientRect();
      if (r.width && (r.right > vw + 1 || r.left < -1) && !clipped(el) && getComputedStyle(el).position !== "fixed") {
        out.push(`${el.tagName.toLowerCase()}[data-testid=${el.getAttribute("data-testid")}] ${Math.round(r.left)}..${Math.round(r.right)}`);
      }
    }
    return { scrollWidth: document.documentElement.scrollWidth, offenders: out.slice(0, 5) };
  }, width);
}

async function settle(page) {
  await page.waitForLoadState("networkidle").catch(() => {});
  // Charts initialise as they near the viewport; walk the page once.
  await page.evaluate(async () => {
    for (let y = 0; y < document.body.scrollHeight; y += 500) {
      window.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 60));
    }
    window.scrollTo(0, 0);
  });
  await page.waitForTimeout(300);
}

const WIDTHS = [360, 390, 768, 1280, 1920];
const ROUTES = ["/", "/?view=breakdown", "/activity", "/compare", "/risk"];

for (const width of WIDTHS) {
  for (const scheme of ["light", "dark"]) {
    test(`no page is wider than the screen at ${width}px (${scheme})`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width, height: 860 });
      await fixtureApi(page);
      for (const route of ROUTES) {
        await page.goto(route);
        await settle(page);
        const { scrollWidth, offenders } = await overflow(page, width);
        expect(scrollWidth, `${route} scrolls sideways at ${width}px: ${offenders.join("; ")}`).toBeLessThanOrEqual(width);
      }
    });
  }
}

for (const width of [390, 1280]) {
  test(`chart tooltips stay on screen at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 860 });
    await fixtureApi(page);
    await page.goto("/");
    await settle(page);
    const charts = page.locator("[data-testid=dashboard-trend] canvas, [data-testid=dashboard-allocation] canvas");
    expect(await charts.count()).toBeGreaterThan(0);
    for (const canvas of await charts.all()) {
      await canvas.scrollIntoViewIfNeeded();
      const box = await canvas.boundingBox();
      // The edges are where an unconfined tooltip escapes.
      for (const fx of [0.04, 0.5, 0.96]) {
        for (const fy of [0.15, 0.5]) {
          await page.mouse.move(box.x + box.width * fx, box.y + box.height * fy);
          await page.waitForTimeout(120);
          const tip = await page.evaluate(() => {
            const el = [...document.querySelectorAll("div")].find(
              (d) => d.style.zIndex === "9999999" && d.style.display !== "none" && d.offsetWidth
            );
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return { left: r.left, right: r.right };
          });
          if (tip) {
            expect(tip.left, "tooltip past the left edge").toBeGreaterThanOrEqual(0);
            expect(tip.right, "tooltip past the right edge").toBeLessThanOrEqual(width);
          }
        }
      }
    }
  });
}

test("the Persian UI leaves no English on Home or in the add dialog", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 860 });
  await fixtureApi(page, { lang: "fa" });
  const english = async () =>
    page.evaluate(() => {
      const found = new Set();
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const el = n.parentElement;
        if (!el || el.closest("script, style, .sr-only, [aria-hidden=true]") || !el.offsetParent) continue;
        const text = n.textContent.trim();
        // Three Latin letters in a row is a word; "T", "$" and digits are not.
        if (/[A-Za-z]{3,}/.test(text)) found.add(text);
      }
      return [...found];
    });

  await page.goto("/");
  await settle(page);
  // The app's name and the fixture's email address are not interface text.
  const allowed = new Set(["Holdings", "fixture@example.test"]);
  expect((await english()).filter((s) => !allowed.has(s))).toEqual([]);

  await page.getByTestId("tab-add-trade").click();
  await expect(page.getByTestId("add-transaction")).toBeVisible();
  // Two portfolios and none in scope: the portfolio is the first question.
  await page.locator("[data-testid^=add-transaction-portfolio-]").first().click();
  await page.getByTestId("add-transaction-category-gold").click();
  expect((await english()).filter((s) => !allowed.has(s))).toEqual([]);
});

test("the Persian sign-in page leaves no English, and offers the language switch", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 860 });
  // Signed out: the first page every visitor sees, and the one the
  // translation pass first missed because it only crawled signed-in pages.
  await page.addInitScript(() => localStorage.setItem("lang", "fa"));
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/registration/") {
      return route.fulfill({ status: 200, contentType: "application/json", body: '{"registration_open":true,"self_service_reset":true}' });
    }
    return route.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"Authentication credentials were not provided."}' });
  });
  await page.goto("/login");
  await expect(page.getByTestId("auth-email-input")).toBeVisible();
  await expect(page.getByTestId("lang-toggle")).toBeVisible();
  for (const mode of ["auth-toggle-login", "auth-toggle-register"]) {
    await page.getByTestId(mode).click();
    const english = await page.evaluate(() => {
      const found = new Set();
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const el = n.parentElement;
        if (!el || el.closest("script, style, .sr-only, [aria-hidden=true]") || !el.offsetParent) continue;
        if (/[A-Za-z]{3,}/.test(n.textContent)) found.add(n.textContent.trim());
      }
      for (const el of document.querySelectorAll("input[placeholder]")) {
        const v = el.getAttribute("placeholder");
        if (/[A-Za-z]{3,}/.test(v) && !v.includes("@")) found.add(v);
      }
      return [...found];
    });
    // The app's name; "EN" (two letters) is the switch back.
    expect(english.filter((s) => s !== "Holdings"), mode).toEqual([]);
  }
});

test("switching language on an open page re-renders all of its text", async ({ page }) => {
  // A component that calls translate() directly, without useT/useLang, keeps
  // the old language until something else re-renders it. Price history did.
  await page.setViewportSize({ width: 1440, height: 900 });
  await fixtureApi(page, { lang: "en" });
  await page.route("**/api/prices/history/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
  );
  await page.goto("/research?view=prices");
  const picker = page.getByRole("combobox", { name: "Asset", exact: true });
  await expect(picker).toBeVisible();
  await page.getByTestId("lang-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  await expect(picker).toHaveCount(0);
  await expect(page.locator("option", { hasText: "Choose…" })).toHaveCount(0);
});

/**
 * Lighthouse against the AUTHENTICATED routes.
 *
 * `scripts/lhci.sh` audits the sign-in page, which is the only route reachable
 * without a backend -- so it can gate CI, but it measures a page that loads
 * almost nothing. Every interesting cost in this app is behind the JWT: the
 * chart chunk, the tables, the polling.
 *
 * Auditing those needs a real session, and the session cannot be faked here.
 * `api.js` keeps the access token in memory only and restores it from an
 * httpOnly refresh cookie plus a `lattice_session` localStorage flag, so there
 * is no token to inject. The way through is to sign in with a real browser and
 * let Lighthouse audit inside it: it opens its own tab against the same origin
 * in the same browser, which is why the cookie and the flag carry over.
 *
 * Deliberately NOT part of the CI gate. It needs credentials and a live
 * backend, and a gate that depends on either is a gate that goes red for
 * reasons that are not the commit's fault. What CI enforces instead is the
 * bundle budget in `lighthouserc.json` -- the thing that actually regresses.
 *
 * `lighthouse` and `chrome-launcher` are installed on demand by the npm script
 * rather than being devDependencies: CI runs `npm ci` on every push and does
 * not run this, so carrying them permanently would slow every build for a tool
 * used by hand.
 *
 *   BASE_URL=https://... LH_EMAIL=... LH_PASSWORD=... npm run test:lighthouse:auth
 */
import lighthouse from "lighthouse";
import * as chromeLauncher from "chrome-launcher";
import puppeteer from "puppeteer-core";
import { writeFileSync, mkdirSync } from "node:fs";

const BASE = process.env.BASE_URL || "http://localhost:5173";
const EMAIL = process.env.LH_EMAIL;
const PASSWORD = process.env.LH_PASSWORD;

if (!EMAIL || !PASSWORD) {
  console.error("LH_EMAIL and LH_PASSWORD are required.");
  process.exit(2);
}

// Routes worth measuring, and why each earns its run. These are the REAL paths
// from App.jsx -- the dashboard lives at "/", and there is no "/dashboard".
// Auditing invented paths still renders (the `*` catch-all serves the app), so
// the scores look plausible while measuring a URL the router never defined.
//   /          the dashboard: chart chunk plus the holdings table, the heaviest
//   /ledger    the longest list, so the best CLS and long-task signal
//   /optimal   the optimizer's charts, which render after a slow API call
const ROUTES = (process.env.LH_ROUTES || "/,/ledger,/optimal").split(",");

// SEO is deliberately absent.
//
// Every authenticated route is `Disallow`ed in robots.txt, which drops the SEO
// category to 63 on the single audit "Page is blocked from indexing" -- and
// that is the CORRECT state. These pages return the sign-in shell to a crawler
// and there is nothing there to index. Scoring SEO 100 on them would mean
// inviting crawlers into the app, so the number is not a goal here; the public
// sign-in page still enforces seo:1.0 through lighthouserc.json, which is where
// SEO actually means something.
const THRESHOLDS = {
  performance: 0.9,
  accessibility: 1.0,
  "best-practices": 1.0,
};

// Chrome is launched once and BOTH tools attach to it, which is the only way
// the session survives from sign-in into the audit. Driving it with
// puppeteer-core rather than Playwright is deliberate: Lighthouse bundles
// puppeteer-core and speaks its CDP dialect, whereas Playwright manages its own
// channel and the two disagree about the /json/version URL shape.
// No fixed port: chrome-launcher picks a free one. Hardcoding 9222 collides
// with any Chrome the developer already has open with debugging enabled -- which
// answers on that port but serves a different protocol surface, so the failure
// arrives as a bare "404 on /json/version" rather than "that port is taken".
const chrome = await chromeLauncher.launch({
  chromeFlags: ["--headless=new", "--no-sandbox", "--disable-gpu"],
});
const browser = await puppeteer.connect({
  browserURL: `http://127.0.0.1:${chrome.port}`,
  defaultViewport: null,
});

try {
  const page = (await browser.pages())[0] || (await browser.newPage());

  console.log(`==> signing in at ${BASE}`);
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="auth-email-input"]', { timeout: 30000 });
  await page.type('[data-testid="auth-email-input"]', EMAIL);
  await page.type('[data-testid="auth-password-input"]', PASSWORD);
  await page.click('[data-testid="auth-submit"]');
  // Wait for the app shell, not a timeout: the nav only mounts once the token
  // is in hand, so its presence is the actual proof that sign-in worked.
  await page.waitForSelector('[data-testid="nav"]', { timeout: 30000 });
  console.log("    signed in");

  mkdirSync(".lighthouseci", { recursive: true });
  const summary = [];
  let failed = false;

  for (const route of ROUTES) {
    const url = new URL(route, BASE).toString();
    console.log(`\n==> auditing ${route}`);

    const result = await lighthouse(
      url,
      {
        port: chrome.port,
        output: "html",
        logLevel: "error",
        // Desktop, matching lighthouserc.json. Throttling a desktop app as
        // mobile 4G measures a device nobody uses this on and makes the number
        // incomparable to the sign-in audit.
        preset: "desktop",
        disableStorageReset: true, // keep the session we just established
      },
    );

    const { categories, audits } = result.lhr;
    const scores = Object.fromEntries(
      Object.entries(categories).map(([k, v]) => [k, Math.round(v.score * 100)]),
    );
    const metrics = {
      FCP: audits["first-contentful-paint"].displayValue,
      LCP: audits["largest-contentful-paint"].displayValue,
      TBT: audits["total-blocking-time"].displayValue,
      CLS: audits["cumulative-layout-shift"].displayValue,
    };
    console.log("   ", JSON.stringify(scores));
    console.log("   ", JSON.stringify(metrics));

    const slug = route.replace(/\W+/g, "-").replace(/^-|-$/g, "") || "root";
    writeFileSync(`.lighthouseci/authenticated-${slug}.html`, result.report);

    for (const [category, min] of Object.entries(THRESHOLDS)) {
      const score = categories[category].score;
      if (score < min) {
        failed = true;
        console.log(
          `    FAIL ${category} ${Math.round(score * 100)} < ${Math.round(min * 100)}`,
        );
        // The failing audits, not just the score -- a bare number tells you
        // nothing about what to fix.
        for (const ref of categories[category].auditRefs) {
          const audit = audits[ref.id];
          if (audit && audit.score !== null && audit.score < 1 && ref.weight > 0) {
            console.log(`      - ${ref.id}: ${audit.title}`);
            // The offending nodes, not just the audit name. "color-contrast
            // fails" is unactionable; the selector and the two colours are the
            // whole fix.
            for (const item of audit.details?.items?.slice(0, 6) ?? []) {
              const node = item.node ?? item.subItems?.items?.[0]?.node;
              if (node) {
                console.log(`          ${node.selector}`);
                if (node.explanation) console.log(`            ${node.explanation}`);
              } else if (item.url) {
                console.log(`          ${item.url}`);
              }
            }
          }
        }
      }
    }
    summary.push({ route, scores, metrics });
  }

  console.log("\n==> summary");
  console.table(
    summary.map((s) => ({ route: s.route, ...s.scores, ...s.metrics })),
  );
  console.log("reports written to .lighthouseci/authenticated-*.html");

  process.exitCode = failed ? 1 : 0;
} finally {
  await browser.close();
  await chrome.kill();
}

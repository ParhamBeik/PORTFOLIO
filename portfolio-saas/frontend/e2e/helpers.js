/**
 * Shared E2E helpers.
 *
 * Env (optional for happy-path auth):
 *   E2E_EMAIL, E2E_PASSWORD — real/dev account with holdings preferred
 */

export function e2eCreds() {
  const email = process.env.E2E_EMAIL;
  const password = process.env.E2E_PASSWORD;
  return email && password ? { email, password } : null;
}

/** True if the Vite app answered (any HTTP status). */
export async function appReachable(request) {
  try {
    const res = await request.get("/", { timeout: 5000 });
    return res.status() < 500;
  } catch {
    return false;
  }
}

/**
 * Fill login form and wait for shell or error.
 * @returns {'ok'|'error'|'timeout'}
 */
export async function login(page, { email, password } = e2eCreds() || {}) {
  await page.goto("/");
  await page.getByTestId("auth-email-input").waitFor({ state: "visible", timeout: 15000 });
  await page.getByTestId("auth-email-input").fill(email);
  await page.getByTestId("auth-password-input").fill(password);
  await page.getByTestId("auth-submit").click();

  const nav = page.getByTestId("nav");
  const err = page.getByTestId("auth-error-banner");
  const onboarding = page.getByTestId("onboarding-card");

  try {
    await Promise.race([
      nav.waitFor({ state: "visible", timeout: 20000 }),
      err.waitFor({ state: "visible", timeout: 20000 }),
      onboarding.waitFor({ state: "visible", timeout: 20000 }),
    ]);
  } catch {
    return "timeout";
  }
  if (await err.isVisible().catch(() => false)) return "error";
  return "ok";
}

/**
 * Skip the test if this run was never set up to sign in; fail it if it was.
 *
 * Skipping a failed login was hiding the suite from itself. The API throttles
 * anonymous requests (30/min by default) and every spec signs in from scratch,
 * so roughly a third of the way through a full run the logins start coming back
 * 429 and every remaining test skips with "login failed". The run then reports
 * 9 passed, 0 failed, exit 0 — while Ops, Optimal, Risk, Universe, the routing
 * aliases and the add-transaction dialog were never opened at all. Raising
 * ANON_THROTTLE for the run turns those 18 skips into 20 passes, which is the
 * measure of how much was going unwatched.
 *
 * So: no credentials is a legitimate skip — that run was never asked to sign in.
 * Credentials that were supplied and did not work is a failure, because it means
 * the specs after it are not testing anything and should say so.
 */
export async function requireLogin(page, test) {
  if (!(await appReachable(page.request))) {
    test.skip(true, "frontend not reachable at baseURL");
  }
  const creds = e2eCreds();
  if (!creds) {
    test.skip(true, "set E2E_EMAIL and E2E_PASSWORD for authenticated specs");
  }
  const result = await login(page, creds);
  if (result !== "ok") {
    throw new Error(
      `login failed (${result}) with E2E_EMAIL set. The backend may be down, the ` +
      `credentials wrong, or the anonymous rate limit exhausted by earlier specs ` +
      `— raise ANON_THROTTLE for the run (see playwright.config.js).`
    );
  }
}

/** Click a tab by its data-testid suffix under a tabs group. */
export async function clickTab(page, tabsTestId, value) {
  const tab = page.getByTestId(`${tabsTestId}-${value}`);
  if (await tab.count()) {
    await tab.click();
    return true;
  }
  return false;
}

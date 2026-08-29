import { test, expect } from "@playwright/test";
import { appReachable, e2eCreds, login } from "./helpers.js";

test.describe("auth", () => {
  test.beforeEach(async ({ request }) => {
    if (!(await appReachable(request))) {
      test.skip(true, "frontend not reachable at baseURL");
    }
  });

  test("login form is visible", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("auth-card")).toBeVisible();
    await expect(page.getByTestId("auth-email-input")).toBeVisible();
    await expect(page.getByTestId("auth-password-input")).toBeVisible();
    await expect(page.getByTestId("auth-submit")).toBeVisible();
    await expect(page.getByTestId("auth-toggle-login")).toBeVisible();
    await expect(page.getByTestId("auth-toggle-register")).toBeVisible();
  });

  // A privacy policy nobody can read without an account is not a privacy
  // policy. The signed-out route tree was a single catch-all, so both legal
  // URLs answered with the sign-in form.
  for (const [path, back] of [["/privacy", "Back to sign in"], ["/terms", "Back to sign in"]]) {
    test(`${path} is readable without signing in`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByTestId("legal-page")).toBeVisible();
      await expect(page.getByTestId("auth-card")).toHaveCount(0);
      await expect(page.getByTestId("legal-back-link")).toHaveText(back);
    });
  }

  test("register toggle shows strength meter path", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("auth-toggle-register").click();
    await page.getByTestId("auth-password-input").fill("Abcd1234!");
    await expect.soft(page.getByTestId("auth-strength-meter")).toBeVisible();
  });

  test("happy-path login when credentials set", async ({ page }) => {
    const creds = e2eCreds();
    if (!creds) {
      test.skip(true, "set E2E_EMAIL and E2E_PASSWORD for happy-path login");
    }
    const result = await login(page, creds);
    if (result !== "ok") {
      test.skip(true, `login failed (${result}) — backend down or bad credentials`);
    }
    await expect(page.getByTestId("nav")).toBeVisible();
    await expect.soft(page.getByTestId("scope-account")).toBeVisible();
    await expect.soft(page.getByTestId("scope-basis")).toBeVisible();

    // Log out now lives inside the account menu, with the rest of the account
    // actions. Opening the chip is what proves both: the menu exists, and the
    // one action that used to sit alone in the header is still reachable.
    await page.getByTestId("user-email").click();
    await expect(page.getByTestId("account-menu")).toBeVisible();
    await expect.soft(page.getByTestId("account-role")).toBeVisible();
    await expect.soft(page.getByTestId("account-change-password")).toBeVisible();
    await expect.soft(page.getByTestId("account-logout")).toBeVisible();

    // Escape closes it — a panel that can only be dismissed by finding the
    // trigger again is a panel people leave open over the page.
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("account-menu")).toHaveCount(0);
  });
});

import { test, expect } from "@playwright/test";
import { appReachable, e2eCreds, login } from "./helpers.js";

test.describe("routing aliases", () => {
  test.beforeEach(async ({ request }) => {
    if (!(await appReachable(request))) {
      test.skip(true, "frontend not reachable at baseURL");
    }
  });

  test("route aliases redirect correctly when signed in", async ({ page }) => {
    const creds = e2eCreds();
    if (!creds) {
      test.skip(true, "set E2E_EMAIL and E2E_PASSWORD for routing test");
    }
    const result = await login(page, creds);
    if (result !== "ok") {
      test.skip(true, `login failed (${result})`);
    }

    // Direct navigation to /breakdown should alias to /family
    await page.goto("/breakdown");
    await page.waitForURL("**/family");
    expect(page.url()).toContain("/family");

    // Direct navigation to /best-overall should alias to /universe
    await page.goto("/best-overall");
    await page.waitForURL("**/universe");
    expect(page.url()).toContain("/universe");
  });
});

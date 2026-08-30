import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("routing aliases", () => {
  // Was an inline copy of `requireLogin` that skipped on a failed sign-in, so
  // this spec reported success without ever visiting either alias.
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  test("route aliases redirect correctly when signed in", async ({ page }) => {
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

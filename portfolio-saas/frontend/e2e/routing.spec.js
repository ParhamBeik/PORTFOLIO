import { test, expect } from "@playwright/test";
import { requireLogin } from "./helpers.js";

test.describe("routing aliases", () => {
  // Was an inline copy of `requireLogin` that skipped on a failed sign-in, so
  // this spec reported success without ever visiting either alias.
  test.beforeEach(async ({ page }) => {
    await requireLogin(page, test);
  });

  test("route aliases redirect correctly when signed in", async ({ page }) => {
    // Legacy breakdown opens the Portfolio breakdown section.
    await page.goto("/breakdown?account=1#allocation");
    await page.waitForURL("**/?account=1&view=breakdown#allocation");
    expect(page.url()).toContain("/?account=1&view=breakdown#allocation");

    // Legacy market optimization opens the Guidance benchmark section.
    await page.goto("/best-overall");
    await page.waitForURL("**/guidance?view=benchmark");
    expect(page.url()).toContain("/guidance?view=benchmark");
  });
});

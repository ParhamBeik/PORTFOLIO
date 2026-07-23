import { expect, test } from "@playwright/test";

const PASSWORD = "Sup3rSecret!";

test.describe("Redesigned Auth & Registration Flow", () => {
  test("direct navigation to /login and /register routes", async ({ page }) => {
    await page.goto("/register");
    await expect(page.getByRole("tab", { name: "Create account" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByLabel("First Name")).toBeVisible();

    await page.goto("/login");
    await expect(page.getByRole("tab", { name: "Sign in" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByLabel("Email Address")).toBeVisible();
    await expect(page.getByLabel("First Name")).toHaveCount(0);
  });

  test("tab switching updates URL seamlessly", async ({ page }) => {
    await page.goto("/login");
    await page.getByRole("tab", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/register$/);
    await expect(page.getByRole("textbox", { name: "Confirm Password" })).toBeVisible();

    await page.getByRole("tab", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/login$/);
  });

  test("autofill demo account credentials fills form and allows fast login", async ({ page }) => {
    await page.goto("/login");
    await page.getByRole("button", { name: /Autofill Demo Credentials/i }).click();

    await expect(page.getByLabel("Email Address")).toHaveValue("demo@portfolio.local");
    await expect(page.getByLabel("Password", { exact: true })).toHaveValue("demo12345");
    await page.getByRole("button", { name: "Sign In" }).click();

    await expect(page.getByRole("link", { name: "Portfolio", exact: true })).toBeVisible();
  });

  test("real-time validation: email format, password strength meter, confirmation match", async ({ page }) => {
    await page.goto("/register");

    // Submit button should be disabled when fields are empty
    await expect(page.getByRole("button", { name: "Create Account" })).toBeDisabled();

    // Fill invalid email
    await page.getByLabel("Email Address").fill("invalid-email");
    await expect(page.getByText("Invalid email")).toBeVisible();
    await expect(page.getByText("Please enter a valid email address")).toBeVisible();

    // Fill valid email
    await page.getByLabel("Email Address").fill("user@example.com");
    await expect(page.getByText("✓ Valid email")).toBeVisible();

    // Fill First Name
    await page.getByLabel("First Name").fill("John");
    await page.getByLabel("Last Name").fill("Doe");

    // Fill weak password
    await page.getByLabel("Password", { exact: true }).fill("123");
    await expect(page.getByText(/Strength: Weak/i)).toBeVisible();
    await expect(page.getByText("At least 8 characters")).toBeVisible();

    // Fill strong password
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await expect(page.getByText(/Strength: Strong/i)).toBeVisible();

    // Check confirm password mismatch
    await page.getByRole("textbox", { name: "Confirm Password" }).fill("Mismatch123!");
    await expect(page.getByText("Mismatch")).toBeVisible();
    await expect(page.getByText("Passwords do not match.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Create Account" })).toBeDisabled();

    // Match confirm password
    await page.getByRole("textbox", { name: "Confirm Password" }).fill(PASSWORD);
    await expect(page.getByText("✓ Match")).toBeVisible();
    await expect(page.getByRole("button", { name: "Create Account" })).toBeEnabled();
  });

  test("show and hide password visibility toggle", async ({ page }) => {
    await page.goto("/login");
    const pwInput = page.getByLabel("Password", { exact: true });

    await pwInput.fill("SecretPassword123");
    await expect(pwInput).toHaveAttribute("type", "password");

    await page.getByRole("button", { name: "Show password" }).click();
    await expect(pwInput).toHaveAttribute("type", "text");

    await page.getByRole("button", { name: "Hide password" }).click();
    await expect(pwInput).toHaveAttribute("type", "password");
  });

  test("full account creation flow with first and last name", async ({ page }) => {
    await page.goto("/register");
    const testEmail = `e2e-reg-${Date.now()}@portfolio.local`;

    await page.getByLabel("First Name").fill("Alice");
    await page.getByLabel("Last Name").fill("Smith");
    await page.getByLabel("Email Address").fill(testEmail);
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByRole("textbox", { name: "Confirm Password" }).fill(PASSWORD);

    await page.getByRole("button", { name: "Create Account" }).click();
    await expect(page.getByRole("link", { name: "Portfolio", exact: true })).toBeVisible();
    await expect(page.getByText(testEmail)).toBeVisible();
  });

  test("invalid credentials displays clear human-readable error banner", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Email Address").fill("nonexistent@portfolio.local");
    await page.getByLabel("Password", { exact: true }).fill("WrongPassword123!");
    await page.getByRole("button", { name: "Sign In" }).click();

    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("Invalid email address or password");
  });
});

import { expect, test } from "@playwright/test";

const PASSWORD = "X7#mK9$vP2qL5wZ8!";
const NEW_PASSWORD = "V9!pL4#wK8$mZ2qX!";

async function registerUser(page, email) {
  await page.goto("/register");
  await page.getByLabel("First Name").fill("TestUser");
  await page.getByLabel("Email Address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("textbox", { name: "Confirm Password" }).fill(PASSWORD);
  const submitBtn = page.locator("button.primary", { hasText: "Create Account" });
  await expect(submitBtn).toBeEnabled();
  await submitBtn.click();

  try {
    await expect(page.getByRole("link", { name: "Portfolio", exact: true })).toBeVisible({ timeout: 5000 });
  } catch (err) {
    const errorBanner = await page.locator(".error-banner").textContent().catch(() => "none");
    console.error("Registration failed. Error banner:", errorBanner);
    throw err;
  }
}

async function loginUser(page, email, password = PASSWORD) {
  await page.goto("/login");
  await page.getByLabel("Email Address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.locator("button.primary", { hasText: "Sign In" }).click();
  await expect(page.getByRole("link", { name: "Portfolio", exact: true })).toBeVisible();
}

test("user can navigate to Profile, view and update personal information", async ({ page }) => {
  const userEmail = `e2e-profile-${Date.now()}@portfolio.local`;

  // Register new user
  await registerUser(page, userEmail);

  // Navigate to Profile page via tab link
  await page.getByRole("link", { name: "Profile", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Account & Security Settings" })).toBeVisible();
  await expect(page.getByLabel("Email Address")).toHaveValue(userEmail);

  // Update first name and last name
  await page.getByLabel("First Name").fill("Jane");
  await page.getByLabel("Last Name").fill("Doe");
  await page.getByRole("button", { name: "Save Changes" }).click();
  await expect(page.getByRole("status")).toContainText("Profile information updated successfully.");

  // Reload page to verify persistence
  await page.reload();
  await expect(page.getByLabel("First Name")).toHaveValue("Jane");
  await expect(page.getByLabel("Last Name")).toHaveValue("Doe");
});

test("user can change password and log in with new password", async ({ page }) => {
  const userEmail = `e2e-pass-${Date.now()}@portfolio.local`;

  // Register new user
  await registerUser(page, userEmail);

  // Navigate to Profile
  await page.getByRole("link", { name: "Profile", exact: true }).click();

  // Attempt invalid current password
  await page.getByLabel("Current Password").fill("WrongCurrentPass123!");
  await page.getByLabel("New Password", { exact: true }).fill(NEW_PASSWORD);
  await page.getByLabel("Confirm New Password").fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Update Password" }).click();
  await expect(page.getByRole("status")).toContainText("Current password is incorrect.");

  // Change password with correct current password
  await page.getByLabel("Current Password").fill(PASSWORD);
  await page.getByLabel("New Password", { exact: true }).fill(NEW_PASSWORD);
  await page.getByLabel("Confirm New Password").fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Update Password" }).click();
  await expect(page.getByRole("status")).toContainText("Password updated successfully.");


  // Logout and verify sign in with old password fails, sign in with new password succeeds
  await page.getByRole("button", { name: "Logout" }).click();
  await expect(page).toHaveURL(/\/login$/);

  // Old password fails
  await page.getByLabel("Email Address").fill(userEmail);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign In" }).click();
  await expect(page.getByText(/Invalid email address or password/)).toBeVisible();

  // New password succeeds
  await loginUser(page, userEmail, NEW_PASSWORD);
  await expect(page.getByRole("link", { name: "Profile", exact: true })).toBeVisible();
});

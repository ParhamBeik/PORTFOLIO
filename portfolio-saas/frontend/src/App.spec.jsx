import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("./api.js", () => ({
  SESSION_EXPIRED_EVENT: "session-expired",
  auth: { tokens: null },
  confirmPasswordReset: vi.fn(),
  login: vi.fn(),
  me: vi.fn(async () => ({ id: 1, email: "member@example.com" })),
  register: vi.fn(),
  registrationStatus: vi.fn(async () => ({ registration_open: true, self_service_reset: true })),
  requestPasswordReset: vi.fn(),
  restoreSession: vi.fn(async () => "access-token"),
  sessionExpiry: { set: vi.fn() },
}));

const { default: App } = await import("./App.jsx");
const api = await import("./api.js");

afterEach(() => {
  cleanup();
  api.restoreSession.mockReset();
  api.restoreSession.mockResolvedValue("access-token");
  api.registrationStatus.mockReset();
  api.registrationStatus.mockResolvedValue({ registration_open: true, self_service_reset: true });
});

it("keeps an emailed reset link reachable when a session already exists", async () => {
  window.history.pushState({}, "", "/reset-password?uid=MQ&token=token");
  render(<App />);
  expect(await screen.findByTestId("reset-card")).toBeInTheDocument();
  await waitFor(() => expect(document.title).toBe("Reset password — Holdings"));
});

it("opens create-account mode for a signed-out /signup deep link", async () => {
  api.restoreSession.mockResolvedValueOnce(null);
  window.history.pushState({}, "", "/signup");

  render(<App />);

  expect(await screen.findByTestId("auth-toggle-register")).toHaveAttribute(
    "aria-selected",
    "true"
  );
  await waitFor(() => expect(document.title).toBe("Create account — Holdings"));
});

it("sends a signed-out protected deep link to login with its return path", async () => {
  api.restoreSession.mockResolvedValueOnce(null);
  window.history.pushState({}, "", "/ledger?range=90#entry-7");

  render(<App />);

  await screen.findByTestId("auth-card");
  expect(window.location.pathname).toBe("/login");
  expect(new URLSearchParams(window.location.search).get("next")).toBe(
    "/ledger?range=90#entry-7"
  );
});

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("./api.js", () => ({
  SESSION_EXPIRED_EVENT: "session-expired",
  confirmPasswordReset: vi.fn(),
  me: vi.fn(async () => ({ id: 1, email: "member@example.com" })),
  restoreSession: vi.fn(async () => "access-token"),
}));

const { default: App } = await import("./App.jsx");

afterEach(cleanup);

it("keeps an emailed reset link reachable when a session already exists", async () => {
  window.history.pushState({}, "", "/reset-password?uid=MQ&token=token");
  render(<App />);
  expect(await screen.findByTestId("reset-card")).toBeInTheDocument();
  expect(document.title).toBe("Reset password — Holdings");
});

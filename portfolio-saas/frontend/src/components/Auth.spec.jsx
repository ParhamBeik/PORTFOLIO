/**
 * Integration test at the component boundary: the forgot-password path
 * on the sign-in card. Pyramid: this is cheaper than e2e for the form
 * state machine; e2e still covers that the link is on the real page.
 */
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api.js", () => ({
  auth: { tokens: null },
  login: vi.fn(),
  me: vi.fn(),
  register: vi.fn(),
  requestPasswordReset: vi.fn(),
  sessionExpiry: { set: vi.fn() },
}));

const { default: Auth } = await import("./Auth.jsx");
const api = await import("../api.js");

afterEach(cleanup);

describe("Auth recovery", () => {
  it("exposes a main landmark on the sign-in card", () => {
    render(<Auth onAuthed={() => {}} />);
    expect(document.querySelector("main")).not.toBeNull();
  });

  it("sends a reset request without revealing whether the email exists", async () => {
    api.requestPasswordReset.mockResolvedValue({
      detail: "If an account exists for that email, a reset link has been sent.",
    });
    render(<Auth onAuthed={() => {}} />);
    fireEvent.click(screen.getByTestId("auth-forgot"));
    fireEvent.change(screen.getByTestId("auth-email-input"), {
      target: { value: "member@example.com" },
    });
    fireEvent.click(screen.getByTestId("auth-submit"));
    await waitFor(() => {
      expect(screen.getByTestId("auth-reset-sent")).toBeInTheDocument();
    });
    expect(api.requestPasswordReset).toHaveBeenCalledWith("member@example.com");
  });
});

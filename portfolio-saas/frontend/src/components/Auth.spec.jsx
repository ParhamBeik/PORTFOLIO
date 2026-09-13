/**
 * Integration test at the component boundary: the forgot-password path
 * on the sign-in card. Pyramid: this is cheaper than e2e for the form
 * state machine; e2e still covers that the link is on the real page.
 */
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api.js", () => ({
  auth: { tokens: null },
  login: vi.fn(),
  me: vi.fn(),
  register: vi.fn(),
  registrationStatus: vi.fn(),
  requestPasswordReset: vi.fn(),
  sessionExpiry: { set: vi.fn() },
}));

const { default: Auth } = await import("./Auth.jsx");
const api = await import("../api.js");

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  api.registrationStatus.mockResolvedValue({
    registration_open: true,
    self_service_reset: true,
  });
});

describe("Auth recovery", () => {
  it("exposes a main landmark on the sign-in card", () => {
    render(<Auth onAuthed={() => {}} />);
    expect(document.querySelector("main")).not.toBeNull();
  });

  it("hides account creation when the server gate is closed", async () => {
    api.registrationStatus.mockResolvedValue({ registration_open: false });
    render(<Auth onAuthed={() => {}} />);

    await waitFor(() => expect(api.registrationStatus).toHaveBeenCalled());
    expect(screen.queryByTestId("auth-toggle-register")).not.toBeInTheDocument();
  });

  it("creates an account only after every password requirement passes", async () => {
    api.register.mockResolvedValue({ access: "token", session_expires_at: "2026-10-01" });
    api.me.mockResolvedValue({ id: 2, email: "new@example.com" });
    const onAuthed = vi.fn();
    render(<Auth onAuthed={onAuthed} />);

    fireEvent.click(await screen.findByTestId("auth-toggle-register"));
    fireEvent.change(screen.getByTestId("auth-email-input"), {
      target: { value: "new@example.com" },
    });
    fireEvent.change(screen.getByTestId("auth-password-input"), {
      target: { value: "StrongPass1!" },
    });
    expect(screen.getByTestId("auth-submit")).toBeDisabled();
    fireEvent.change(screen.getByTestId("auth-confirm-password-input"), {
      target: { value: "StrongPass1!" },
    });
    fireEvent.click(screen.getByTestId("auth-submit"));

    await waitFor(() => expect(api.register).toHaveBeenCalledWith("new@example.com", "StrongPass1!"));
    expect(onAuthed).toHaveBeenCalledWith({ id: 2, email: "new@example.com" });
  });

  it("sends a reset request without revealing whether the email exists", async () => {
    api.requestPasswordReset.mockResolvedValue({
      detail: "If an account exists for that email, a reset link has been sent.",
    });
    render(<Auth onAuthed={() => {}} />);
    fireEvent.click(await screen.findByTestId("auth-forgot"));
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

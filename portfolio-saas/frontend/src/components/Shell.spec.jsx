/**
 * The small-screen menu, as an overlay.
 *
 * The header renders its links twice — inline above `lg`, in the drawer below
 * it — and two copies of one component is how duplicate test ids and dead
 * click targets get made. So the assertions here are about exactly that: the
 * canonical `nav-ledger` belongs to the inline rail, the drawer's copy is
 * suffixed, and the drawer does not exist in the DOM until it is opened.
 *
 * The scroll lock is asserted too. A drawer that leaves `body` scrollable lets
 * the page slide around behind it, which on a phone is indistinguishable from
 * the inline panel this replaced.
 */
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api.js", () => ({
  changePassword: vi.fn(),
  deleteAccount: vi.fn(),
  downloadExport: vi.fn(),
  logoutSession: vi.fn(),
  sessionExpiry: { value: null },
  updateProfile: vi.fn(),
}));

const PORTFOLIO = {
  accounts: [{ id: 1, name: "Family" }],
  activeId: null,
  setActive: vi.fn(),
  basis: "nominal_toman",
  setBasis: vi.fn(),
  error: null,
  reload: vi.fn(),
};

vi.mock("./PortfolioContext.jsx", () => ({
  usePortfolio: () => PORTFOLIO,
}));

const { default: Shell } = await import("./Shell.jsx");

const USER = { email: "someone@example.com", is_staff: false };

const mount = () =>
  render(
    <MemoryRouter>
      <Shell user={USER} onLogout={() => {}} onUserChange={() => {}} />
    </MemoryRouter>
  );

afterEach(cleanup);

describe("Shell header", () => {
  it("keeps the scope controls on screen at every width", () => {
    mount();
    // Never inside the menu: without them a phone reads a whole screen of
    // figures without ever saying whose money it was, or in what.
    expect(screen.getByTestId("scope-account")).toBeInTheDocument();
    expect(screen.getByTestId("scope-basis")).toBeInTheDocument();
  });

  it("gives the inline rail the canonical nav ids", () => {
    mount();
    const ledger = screen.getByTestId("nav-ledger");
    expect(ledger).toBeInTheDocument();
    // Exactly one, or a click resolves to whichever copy the DOM happens to
    // hold — and the other copy is hidden and unclickable.
    expect(screen.queryAllByTestId("nav-ledger")).toHaveLength(1);
  });
});

describe("Shell drawer", () => {
  it("does not exist until the menu button is pressed", () => {
    mount();
    expect(screen.queryByTestId("nav-drawer")).toBeNull();

    fireEvent.click(screen.getByTestId("nav-toggle"));

    const drawer = screen.getByTestId("nav-drawer");
    expect(drawer).toHaveAttribute("aria-modal", "true");
    expect(drawer).toHaveAttribute("role", "dialog");
    expect(screen.getByTestId("nav-ledger-mobile")).toBeInTheDocument();
  });

  it("locks the page behind it and releases it on close", () => {
    mount();
    fireEvent.click(screen.getByTestId("nav-toggle"));
    expect(document.body.style.overflow).toBe("hidden");

    fireEvent.click(screen.getByTestId("nav-close"));

    expect(screen.queryByTestId("nav-drawer")).toBeNull();
    expect(document.body.style.overflow).not.toBe("hidden");
  });

  it("closes on Escape, like every other overlay", () => {
    mount();
    fireEvent.click(screen.getByTestId("nav-toggle"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("nav-drawer")).toBeNull();
  });

  it("closes when a link inside it is followed", () => {
    mount();
    fireEvent.click(screen.getByTestId("nav-toggle"));
    fireEvent.click(screen.getByTestId("nav-ledger-mobile"));
    expect(screen.queryByTestId("nav-drawer")).toBeNull();
  });
});

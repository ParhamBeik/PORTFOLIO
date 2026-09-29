/**
 * The small-screen menu, as an overlay.
 *
 * The header renders its links twice — inline above `lg`, in the drawer below
 * it — and two copies of one component is how duplicate test ids and dead
 * click targets get made. So the assertions here are about exactly that: the
 * canonical `nav-activity` belongs to the inline rail, the drawer's copy is
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

const USER = { email: "someone@example.com", role: "user" };

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
    const activity = screen.getByTestId("nav-activity");
    expect(activity).toBeInTheDocument();
    // Exactly one, or a click resolves to whichever copy the DOM happens to
    // hold — and the other copy is hidden and unclickable.
    expect(screen.queryAllByTestId("nav-activity")).toHaveLength(1);
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
    expect(screen.getByTestId("nav-activity-mobile")).toBeInTheDocument();
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
    fireEvent.click(screen.getByTestId("nav-activity-mobile"));
    expect(screen.queryByTestId("nav-drawer")).toBeNull();
  });

  it("leaves focus alone when the shell re-renders under it", () => {
    const { rerender } = mount();
    fireEvent.click(screen.getByTestId("nav-toggle"));

    // A keyboard user, three links into the menu.
    const target = screen.getByTestId("nav-explore-mobile");
    target.focus();
    expect(document.activeElement).toBe(target);

    // Anything that re-renders `Shell` -- a price poll landing, the active
    // portfolio changing. The close callback used to be a fresh closure each
    // time, and it is a dependency of the drawer's focus effect, so the effect
    // tore down (restoring focus to the opener) and re-ran (sending it to the
    // top of the panel) for a re-render that changed nothing on screen.
    rerender(
      <MemoryRouter>
        <Shell user={USER} onLogout={() => {}} onUserChange={() => {}} />
      </MemoryRouter>
    );

    expect(screen.getByTestId("nav-drawer")).toBeInTheDocument();
    expect(document.activeElement).toBe(target);
  });
});

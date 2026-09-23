import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import AccountMenu from "./AccountMenu.jsx";
import Logo from "./Logo.jsx";
import { usePortfolio } from "./PortfolioContext.jsx";
import { Button, ErrorState, Select } from "./ui.jsx";

const APP_NAME = "Holdings";

const PAGES = [
  { to: "/", label: "Portfolio", end: true },
  { to: "/activity", label: "Activity" },
  { to: "/markets", label: "Markets" },
  { to: "/guidance", label: "Guidance" },
];

const BASES = [
  ["nominal_toman", "Nominal Toman"],
  ["real_toman", "Real Toman"],
  ["usd_denominated", "USD"],
  ["usdt_denominated", "USDT"],
];

function MenuIcon({ open }) {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      {open ? (
        <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
      ) : (
        <path d="M3 6h14M3 10h14M3 14h14" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
      )}
    </svg>
  );
}

function NavItem({ to, end, testId, children, onClick }) {
  return (
    <NavLink
      to={to}
      end={end}
      data-testid={testId}
      onClick={onClick}
      className={({ isActive }) => `app-nav-link${isActive ? " is-active" : ""}`}
    >
      {children}
    </NavLink>
  );
}

/**
 * The page links, rendered twice: inline above `lg`, in the drawer below it.
 *
 * Two copies means two sets of test ids, and only one can be the canonical
 * `nav-activity` — a duplicate id resolves to whichever the DOM happens to hold
 * and a hidden one is not clickable. The inline rail keeps the plain names
 * because that is the one on screen at the width the suites run at; the drawer
 * suffixes `-mobile`, the same convention `AccountMenu` already uses.
 */
function NavLinks({ admin, suffix = "", onNavigate }) {
  const id = (label) => `nav-${label.toLowerCase().replace(/\s+/g, "-")}${suffix}`;
  return (
    <>
      {PAGES.map((p) => (
        <NavItem key={p.to} to={p.to} end={p.end} testId={id(p.label)} onClick={onNavigate}>
          {p.label}
        </NavItem>
      ))}
      {admin && (
        <NavItem to="/ops" testId={id("Operations")} onClick={onNavigate}>Operations</NavItem>
      )}
    </>
  );
}

/**
 * The small-screen navigation, as a panel that slides in from the right.
 *
 * It used to be an inline block in the header flow, so opening it PUSHED the
 * whole page down and closing it snapped the page back up — on a phone that
 * reads as the content jumping, not as a menu. A drawer sits over the page
 * instead: nothing below it moves, and the thing that moves is the thing the
 * tap was about.
 *
 * Overlay rules are the ones `Modal` already establishes, for the same reasons:
 * Escape closes it, a click on the scrim closes it, the page behind must not
 * scroll under it, and focus goes in on open and back to the opener on close.
 * A menu that leaves focus behind on <body> restarts a keyboard user at the top
 * of the document every time they open it.
 */
function NavDrawer({ open, onClose, user, onLogout, onUserChange }) {
  const panel = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const opener = document.activeElement;
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panel.current?.querySelector("a, button")?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
      if (opener instanceof HTMLElement && document.contains(opener)) opener.focus();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="app-drawer-scrim lg:hidden"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        id="app-nav-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Menu"
        data-testid="nav-drawer"
        className="app-drawer"
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <span className="text-sm font-semibold">Menu</span>
          <Button
            variant="ghost"
            onClick={onClose}
            aria-label="Close menu"
            data-testid="nav-close"
          >
            ✕
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-4">
          <nav aria-label="Primary" className="app-nav-stack" data-testid="nav-mobile">
            <NavLinks admin={user?.role === "admin"} suffix="-mobile" onNavigate={onClose} />
          </nav>

          {/* Account settings that only exist above the md breakpoint are
              account settings most people never find. */}
          <div className="mt-4 border-t border-border pt-4 md:hidden">
            <AccountMenu
              user={user}
              onLogout={onLogout}
              onUserChange={onUserChange}
              triggerClass="flex w-full"
              panelFill
              testId="user-email-mobile"
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export default function Shell({ user, onLogout, onUserChange }) {
  const { accounts, activeId, setActive, basis, setBasis, error, reload } = usePortfolio();
  const [mobileOpen, setMobileOpen] = useState(false);

  // Stable, because the drawer's focus-and-scroll-lock effect lists it as a
  // dependency. A fresh closure per render tore that effect down and rebuilt it
  // on every `usePortfolio()` change -- a price poll, an account switch -- and
  // the teardown restores focus to the opener while the re-run sends it back to
  // the first nav link. A keyboard user tabbed three links deep was thrown to
  // the top mid-poll. `setMobileOpen` is itself stable, so there is nothing to
  // depend on.
  const closeMobile = useCallback(() => setMobileOpen(false), []);

  return (
    <div className="flex min-h-full flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:m-2 focus:rounded focus:bg-[var(--c-accent-fill)] focus:px-3 focus:py-2 focus:text-white"
      >
        Skip to content
      </a>

      <header className="app-header sticky top-0 z-20 border-b border-border bg-panel/95 backdrop-blur-md">
        <div className="flex min-h-[3.5rem] flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2 lg:min-h-[4.25rem] lg:gap-x-5 lg:gap-y-3 lg:px-6 lg:py-3">
          <NavLink
            to="/"
            className="app-brand group order-1 shrink-0"
            aria-label={`${APP_NAME} home`}
            data-testid="app-brand"
          >
            <span className="flex size-8 items-center justify-center rounded-lg border border-border bg-panel-2 text-accent shadow-sm transition group-hover:border-accent/40 group-hover:bg-[var(--c-accent-fill)]/10 lg:size-9">
              <Logo size={20} title={APP_NAME} />
            </span>
            <span className="flex flex-col leading-tight">
              <span className="text-sm font-semibold tracking-tight text-text lg:text-base">{APP_NAME}</span>
              {/* Below lg the header is fighting for every pixel of height and
                  the strapline is the one thing on it that says nothing. */}
              <span className="hidden text-[11px] text-muted lg:block">Portfolio tracker</span>
            </span>
          </NavLink>

          {/* Which portfolio, priced in what: these two say what every number on
              the page MEANS, so they stay on screen at every width rather than
              hiding behind the menu button. Below `lg` they are the compact
              variant -- caption beside the control instead of above it, smaller
              type -- because the alternative to shrinking them is a header that
              eats 141px of a phone's viewport before any content loads. */}
          <div
            className="app-toolbar order-3 w-full lg:ml-auto lg:w-auto"
            data-testid="header-toolbar"
          >
            <label className="app-toolbar-label">
              <span className="app-toolbar-caption">Portfolio</span>
              <Select
                label="Active portfolio"
                data-testid="scope-account"
                className="app-toolbar-select"
                value={activeId ?? ""}
                onChange={(e) => setActive(e.target.value === "" ? null : Number(e.target.value))}
              >
                <option value="">All portfolios</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                    {a.goal ? ` · ${a.goal}` : ""}
                  </option>
                ))}
              </Select>
            </label>
            <span className="app-toolbar-divider" aria-hidden="true" />
            <label className="app-toolbar-label">
              <span className="app-toolbar-caption">Basis</span>
              <Select
                label="Valuation basis"
                data-testid="scope-basis"
                className="app-toolbar-select"
                value={basis}
                onChange={(e) => setBasis(e.target.value)}
              >
                {BASES.map(([v, l]) => (
                  <option key={v} value={v}>{l}</option>
                ))}
              </Select>
            </label>
          </div>

          {/* From `lg` up there is room for the links inline, and a drawer for
              seven tabs on a desktop would be hiding navigation for no reason.
              The breakpoint lives on `.app-nav-rail` in index.css, not in a
              `hidden lg:flex` here: that utility and the component class are
              both single-class selectors, so the later stylesheet wins and the
              rail rendered stacked inside a phone's header. */}
          <nav
            aria-label="Primary"
            className="app-nav-rail order-2"
            data-testid="nav"
          >
            <NavLinks admin={user?.role === "admin"} />
          </nav>

          <div className="order-2 ml-auto flex items-center gap-2 sm:gap-3 lg:order-4 lg:ml-0">
            {/* Log out lives INSIDE this menu, next to the rest of the account
                actions it belongs with — it was the only one that had a home. */}
            <AccountMenu user={user} onLogout={onLogout} onUserChange={onUserChange} />

            <button
              type="button"
              className="app-header-btn inline-flex items-center justify-center rounded-md border border-border bg-panel-2 p-2 text-text lg:hidden"
              aria-label={mobileOpen ? "Close menu" : "Open menu"}
              aria-expanded={mobileOpen}
              aria-haspopup="dialog"
              aria-controls="app-nav-panel"
              data-testid="nav-toggle"
              onClick={() => setMobileOpen((v) => !v)}
            >
              <MenuIcon open={mobileOpen} />
            </button>
          </div>
        </div>
      </header>

      <NavDrawer
        open={mobileOpen}
        onClose={closeMobile}
        user={user}
        onLogout={onLogout}
        onUserChange={onUserChange}
      />

      {/* `flex-1` alone makes main fill the space LEFT OVER, which lands the
          footer exactly on the fold while the route is still loading. Every
          data route then grows past a screen and pushes the footer out of view
          — one move, no content of its own, and the whole of this app's
          cumulative layout shift (0.05 on Breakdown, Best Overall and My
          Optimal; the footer was the only node either the browser or Lighthouse
          ever reported). Reserving a screen of content puts the footer below
          the fold from the first paint, so its move is no longer a shift a
          reader can see. The pages genuinely shorter than this — the legal
          text, onboarding — pay one header's worth of extra scroll to reach
          it. */}
      <main id="main" tabIndex={-1} className="mx-auto min-h-[100svh] w-full max-w-7xl flex-1 px-4 py-6 lg:px-6">
        {error && (
          <div className="mb-4">
            <ErrorState error={{ message: `Could not load portfolios: ${error}` }} onRetry={reload} />
          </div>
        )}
        <Outlet />
      </main>

      <footer className="border-t border-border px-4 py-3 text-center text-xs text-muted lg:px-6">
        <NavLink to="/privacy" className="hover:text-text">Privacy</NavLink>
        <span className="mx-2">·</span>
        <NavLink to="/terms" className="hover:text-text">Terms</NavLink>
        <span className="mx-2">·</span>
        <span>Informational use only. Not investment advice.</span>
      </footer>
    </div>
  );
}

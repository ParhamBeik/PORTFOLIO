import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { logoutSession } from "../api.js";
import Logo from "./Logo.jsx";
import { usePortfolio } from "./PortfolioContext.jsx";
import { Button, ErrorState, Select } from "./ui.jsx";

const APP_NAME = "Holdings";

const PAGES = [
  { to: "/", label: "Portfolio", end: true },
  { to: "/ledger", label: "Ledger" },
  { to: "/family", label: "Breakdown" },
  { to: "/comparison", label: "Comparison" },
  { to: "/optimal", label: "My Optimal" },
  { to: "/universe", label: "Best Overall" },
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

export default function Shell({ user, onLogout }) {
  const { accounts, activeId, setActive, basis, setBasis, error, reload } = usePortfolio();
  const [mobileOpen, setMobileOpen] = useState(false);

  const doLogout = async () => {
    await logoutSession();
    onLogout();
  };

  const initial = (user?.email || "?").charAt(0).toUpperCase();
  const closeMobile = () => setMobileOpen(false);

  // An opened menu covers the page it was opened from, so it has to be
  // dismissable the way every other overlay is.
  useEffect(() => {
    if (!mobileOpen) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") setMobileOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mobileOpen]);

  return (
    <div className="flex min-h-full flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:m-2 focus:rounded focus:bg-accent focus:px-3 focus:py-2 focus:text-white"
      >
        Skip to content
      </a>

      <header className="app-header sticky top-0 z-20 border-b border-border bg-panel/95 backdrop-blur-md">
        <div className="flex min-h-[4.25rem] flex-wrap items-center gap-x-5 gap-y-3 px-5 py-3 lg:px-6">
          <NavLink
            to="/"
            className="app-brand group order-1 shrink-0"
            aria-label={`${APP_NAME} home`}
            data-testid="app-brand"
          >
            <span className="flex size-9 items-center justify-center rounded-lg border border-border bg-panel-2 text-accent shadow-sm transition group-hover:border-accent/40 group-hover:bg-accent/10">
              <Logo size={22} title={APP_NAME} />
            </span>
            <span className="flex flex-col leading-tight">
              <span className="text-base font-semibold tracking-tight text-text">{APP_NAME}</span>
              <span className="hidden text-[11px] text-muted sm:block">Portfolio tracker</span>
            </span>
          </NavLink>

          {/* Which portfolio, priced in what: these two say what every number on
              the page MEANS, so they sit outside the collapsible panel and stay
              on screen at every width. Inside it, a phone read a whole screen of
              figures without ever saying whose money it was. Below `lg` they
              take a row of their own, under the brand. */}
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

          {/* Below `lg` the page links wrap to a line of their own, and only when
              opened — six tabs inline eat most of a phone's viewport. */}
          <div
            id="app-nav-panel"
            className={`${mobileOpen ? "flex" : "hidden"} order-4 w-full flex-col gap-3 lg:contents`}
          >
            <nav aria-label="Primary" className="app-nav-rail lg:order-2" data-testid="nav">
              {PAGES.map((p) => (
                <NavItem
                  key={p.to}
                  to={p.to}
                  end={p.end}
                  testId={`nav-${p.label.toLowerCase().replace(/\s+/g, "-")}`}
                  onClick={closeMobile}
                >
                  {p.label}
                </NavItem>
              ))}
              {user?.is_staff && (
                <NavItem to="/ops" testId="nav-ops" onClick={closeMobile}>Ops</NavItem>
              )}
            </nav>

            <div className="app-user-chip flex md:hidden" data-testid="user-email-mobile">
              <span className="app-user-avatar" aria-hidden="true">{initial}</span>
              <span className="max-w-[11rem] truncate text-sm text-muted">{user.email}</span>
            </div>

            {/* The icon that opened this turns into a close cross, which is easy
                to miss once the panel has pushed the page down. Escape works too. */}
            <Button
              variant="ghost"
              className="app-header-btn w-full lg:hidden"
              onClick={closeMobile}
              data-testid="nav-close"
            >
              Close menu
            </Button>
          </div>

          <div className="order-2 ml-auto flex items-center gap-2 sm:gap-3 lg:order-4 lg:ml-0">
            <div className="app-user-chip hidden md:flex" data-testid="user-email">
              <span className="app-user-avatar" aria-hidden="true">{initial}</span>
              <span className="max-w-[11rem] truncate text-sm text-muted">{user.email}</span>
            </div>

            <Button variant="ghost" className="app-header-btn" onClick={doLogout} data-testid="logout">
              Log out
            </Button>

            <button
              type="button"
              className="app-header-btn inline-flex items-center justify-center rounded-md border border-border bg-panel-2 p-2 text-text lg:hidden"
              aria-label={mobileOpen ? "Close menu" : "Open menu"}
              aria-expanded={mobileOpen}
              aria-controls="app-nav-panel"
              data-testid="nav-toggle"
              onClick={() => setMobileOpen((v) => !v)}
            >
              <MenuIcon open={mobileOpen} />
            </button>
          </div>
        </div>
      </header>

      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 lg:px-6">
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

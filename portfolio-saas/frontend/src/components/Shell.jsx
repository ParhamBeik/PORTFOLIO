import { useState } from "react";
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
            className="app-brand group shrink-0"
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

          {/* Below `lg` this row wraps to a new line only when opened — the nav
              rail and toolbar are wide enough (6 tabs + 2 selects) that showing
              them inline always eats most of the viewport on a phone. */}
          <div
            id="app-nav-panel"
            className={`${mobileOpen ? "flex" : "hidden"} w-full flex-col gap-3 lg:contents`}
          >
            <nav aria-label="Primary" className="app-nav-rail" data-testid="nav">
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

            <div className="app-toolbar lg:ml-auto" data-testid="header-toolbar">
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

            <div className="app-user-chip flex md:hidden" data-testid="user-email-mobile">
              <span className="app-user-avatar" aria-hidden="true">{initial}</span>
              <span className="max-w-[11rem] truncate text-sm text-muted">{user.email}</span>
            </div>
          </div>

          <div className="ml-auto flex items-center gap-2 sm:gap-3">
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

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

function NavItem({ to, end, testId, children }) {
  return (
    <NavLink
      to={to}
      end={end}
      data-testid={testId}
      className={({ isActive }) => `app-nav-link${isActive ? " is-active" : ""}`}
    >
      {children}
    </NavLink>
  );
}

export default function Shell({ user, onLogout }) {
  const { accounts, activeId, setActive, basis, setBasis, error, reload } = usePortfolio();

  const doLogout = async () => {
    await logoutSession();
    onLogout();
  };

  const initial = (user?.email || "?").charAt(0).toUpperCase();

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

          <nav aria-label="Primary" className="app-nav-rail" data-testid="nav">
            {PAGES.map((p) => (
              <NavItem
                key={p.to}
                to={p.to}
                end={p.end}
                testId={`nav-${p.label.toLowerCase().replace(/\s+/g, "-")}`}
              >
                {p.label}
              </NavItem>
            ))}
            {user?.is_staff && (
              <NavItem to="/ops" testId="nav-ops">Ops</NavItem>
            )}
          </nav>

          <div className="ml-auto flex flex-wrap items-center gap-2 sm:gap-3">
            <div className="app-toolbar" data-testid="header-toolbar">
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

            <div className="app-user-chip hidden md:flex" data-testid="user-email">
              <span className="app-user-avatar" aria-hidden="true">{initial}</span>
              <span className="max-w-[11rem] truncate text-sm text-muted">{user.email}</span>
            </div>

            <Button variant="ghost" className="app-header-btn" onClick={doLogout} data-testid="logout">
              Log out
            </Button>
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

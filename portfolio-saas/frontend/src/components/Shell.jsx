import { NavLink, Outlet } from "react-router-dom";
import { logoutSession } from "../api.js";
import Logo from "./Logo.jsx";
import { usePortfolio } from "./PortfolioContext.jsx";
import { Button, ErrorState, Select } from "./ui.jsx";

const PAGES = [
  { to: "/", label: "Portfolio", end: true },
  { to: "/ledger", label: "Ledger" },
  { to: "/family", label: "Family" },
  { to: "/optimal", label: "My Optimal" },
  { to: "/universe", label: "Best Overall" },
];

const BASES = [
  ["nominal_toman", "Nominal Toman"],
  ["real_toman", "Real Toman"],
  ["usd_denominated", "USD"],
  ["usdt_denominated", "USDT"],
];

export default function Shell({ user, onLogout }) {
  const { accounts, activeId, setActive, basis, setBasis, error, reload } = usePortfolio();

  const doLogout = async () => {
    await logoutSession();
    onLogout();
  };

  return (
    <div className="flex min-h-full flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:m-2 focus:rounded focus:bg-accent focus:px-3 focus:py-2 focus:text-white"
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-10 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border bg-panel px-4 py-2.5">
        <NavLink to="/" className="flex items-center gap-2 font-semibold" aria-label="Lattice home">
          <Logo size={20} />
          <span>Lattice</span>
        </NavLink>

        <nav aria-label="Primary" className="flex gap-1" data-testid="nav">
          {PAGES.map((p) => (
            <NavLink
              key={p.to}
              to={p.to}
              end={p.end}
              data-testid={`nav-${p.label.toLowerCase().replace(" ", "-")}`}
              className={({ isActive }) =>
                `rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  isActive ? "bg-panel-2 text-text" : "text-muted hover:text-text"
                }`
              }
            >
              {p.label}
            </NavLink>
          ))}
          {user?.is_staff && (
            <NavLink
              to="/ops"
              data-testid="nav-ops"
              className={({ isActive }) =>
                `rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  isActive ? "bg-panel-2 text-text" : "text-muted hover:text-text"
                }`
              }
            >
              Ops
            </NavLink>
          )}
        </nav>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Select
            label="Active portfolio"
            data-testid="scope-account"
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

          <Select
            label="Valuation basis"
            data-testid="scope-basis"
            value={basis}
            onChange={(e) => setBasis(e.target.value)}
          >
            {BASES.map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </Select>

          <span className="hidden text-sm text-muted sm:inline" data-testid="user-email">
            {user.email}
          </span>
          <Button onClick={doLogout} data-testid="logout">Log out</Button>
        </div>
      </header>

      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
        {error && (
          <div className="mb-4">
            <ErrorState error={{ message: `Could not load portfolios: ${error}` }} onRetry={reload} />
          </div>
        )}
        <Outlet />
      </main>

      <footer className="border-t border-border px-4 py-3 text-center text-xs text-muted">
        <NavLink to="/privacy" className="hover:text-text">Privacy</NavLink>
        <span className="mx-2">·</span>
        <NavLink to="/terms" className="hover:text-text">Terms</NavLink>
        <span className="mx-2">·</span>
        <span>Informational use only. Not investment advice.</span>
      </footer>
    </div>
  );
}

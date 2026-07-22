import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { auth, me } from "./api.js";
import Auth from "./components/Auth.jsx";
import Portfolio from "./components/Portfolio.jsx";
import Billing from "./components/Billing.jsx";
import MarketData from "./components/MarketData.jsx";
import Logo from "./components/Logo.jsx";
import { PortfolioProvider, usePortfolio } from "./components/PortfolioContext.jsx";

export default function App() {
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!auth.token) return setReady(true);
    me()
      .then(setUser)
      .catch(() => auth.logout())
      .finally(() => setReady(true));
  }, []);

  if (!ready) return <div className="loading">Loading…</div>;

  // The shell (topbar + <Outlet/>) only renders behind auth; /login is standalone
  // so the Zarinpal redirect and unauthed deep links both land cleanly.
  return (
    <BrowserRouter>
      <PortfolioProvider key={user?.id ?? "anonymous"} enabled={Boolean(user)}>
        <Routes>
          <Route
            path="/login"
            element={user ? <Navigate to="/" replace /> : <Auth onAuthed={setUser} />}
          />
          <Route element={user ? <Shell user={user} setUser={setUser} /> : <Navigate to="/login" replace />}>
            <Route index element={<Portfolio user={user} />} />
            <Route path="market" element={<MarketData user={user} />} />
            <Route path="billing" element={<Billing user={user} setUser={setUser} />} />
            {/* Legacy deep links: pick the portfolio, then land on the single page. */}
            <Route path="accounts/:id" element={<AccountRedirect />} />
            <Route path="dashboard" element={<Navigate to="/" replace />} />
            <Route path="insights" element={<Navigate to="/" replace />} />
            <Route path="analytics" element={<Navigate to="/" replace />} />
            <Route path="optimization" element={<Navigate to="/" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </PortfolioProvider>
    </BrowserRouter>
  );
}

// /accounts/:id → set that portfolio active and drop onto the consolidated page.
function AccountRedirect() {
  const { id } = useParams();
  const { setActive } = usePortfolio();
  useEffect(() => {
    const num = Number(id);
    if (Number.isFinite(num)) setActive(num);
  }, [id, setActive]);
  return <Navigate to="/" replace />;
}

function Shell({ user, setUser }) {
  const navigate = useNavigate();
  const { accounts, activeId, setActive } = usePortfolio();

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <Logo size={22} />
          <span className="brand-name">Lattice</span>
        </div>
        {/* The "slider at the top": pick which portfolio the whole page scopes to.
            null = "All portfolios" (the aggregate across every account). */}
        <select
          className="portfolio-select"
          value={activeId ?? ""}
          onChange={(e) => {
            const v = e.target.value;
            setActive(v === "" ? null : Number(v));
          }}
        >
          <option value="">All portfolios</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}{a.goal ? ` · ${a.goal}` : ""}
            </option>
          ))}
        </select>
        <nav className="tabs">
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            Portfolio
          </NavLink>
          <NavLink to="/market" className={({ isActive }) => (isActive ? "active" : "")}>
            Market
          </NavLink>
          <NavLink to="/billing" className={({ isActive }) => (isActive ? "active" : "")}>
            Billing
          </NavLink>
        </nav>
        <div className="userbox">
          <span className="tier-badge">{user.is_pro ? "PRO" : "FREE"}</span>
          <span className="email">{user.email}</span>
          <button
            onClick={() => {
              auth.logout();
              setUser(null);
              navigate("/login");
            }}
          >
            Logout
          </button>
        </div>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}

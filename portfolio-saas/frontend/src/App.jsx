import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { auth, me } from "./api.js";
import Auth from "./components/Auth.jsx";
import Portfolio from "./components/Portfolio.jsx";
import Billing from "./components/Billing.jsx";
import MarketData from "./components/MarketData.jsx";
import Logo from "./components/Logo.jsx";
import OptimizationLayout from "./components/OptimizationLayout.jsx";
import Optimization from "./components/Optimization.jsx";
import Insights from "./components/Insights.jsx";
import Analytics from "./components/Analytics.jsx";
import AdminPortal from "./components/AdminPortal.jsx";
import Profile from "./components/Profile.jsx";
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
            element={<Auth initialMode="login" onAuthed={setUser} currentUser={user} />}
          />
          <Route
            path="/register"
            element={<Auth initialMode="register" onAuthed={setUser} currentUser={user} />}
          />
          <Route
            path="/signup"
            element={<Auth initialMode="register" onAuthed={setUser} currentUser={user} />}
          />
          <Route element={user ? <Shell user={user} setUser={setUser} /> : <Navigate to="/login" replace />}>
            <Route index element={<Portfolio user={user} />} />
            <Route path="market" element={<MarketData user={user} />} />
            <Route path="billing" element={<Billing user={user} setUser={setUser} />} />
            <Route path="profile" element={<Profile user={user} setUser={setUser} />} />
            <Route
              path="admin"
              element={user?.is_staff ? <AdminPortal /> : <Navigate to="/" replace />}
            />


            {/* Legacy deep links */}
            <Route path="accounts/:id" element={<AccountRedirect />} />
            <Route path="dashboard" element={<Navigate to="/" replace />} />
            {/* Pro area: optimization, insights, analytics as sub-routes */}
            <Route path="optimization" element={<OptimizationLayout user={user} />}>
              <Route index element={<Optimization user={user} account={null} />} />
              <Route path="insights" element={<Insights user={user} account={null} />} />
              <Route path="analytics" element={<Analytics user={user} account={null} />} />
            </Route>
            <Route path="insights" element={<Navigate to="/optimization/insights" replace />} />
            <Route path="analytics" element={<Navigate to="/optimization/analytics" replace />} />
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
  const [theme, setTheme] = useState(() => localStorage.getItem("theme") || "dark");

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);

  const toggleTheme = () => setTheme((t) => (t === "dark" ? "light" : "dark"));

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
          <NavLink to="/optimization" className={({ isActive }) => (isActive ? "active" : "")}>
            Optimization
          </NavLink>
          <NavLink to="/billing" className={({ isActive }) => (isActive ? "active" : "")}>
            Billing
          </NavLink>
          <NavLink to="/profile" className={({ isActive }) => (isActive ? "active" : "")}>
            Profile
          </NavLink>
          {user.is_staff && (
            <NavLink to="/admin" className={({ isActive }) => (isActive ? "active" : "")}>
              Admin
            </NavLink>
          )}

        </nav>
        <div className="userbox">
          <button
            type="button"
            className="theme-toggle-btn"
            onClick={toggleTheme}
            title="Toggle theme (Light / Dark)"
            aria-label="Toggle theme mode"
          >
            {theme === "dark" ? "☀️ Light" : "🌙 Dark"}
          </button>
          <span className="tier-badge">{user.is_pro ? "PRO" : "FREE"}</span>
          <NavLink to="/profile" className="email-link" title="Account & Security Settings">
            <span className="email">{user.email}</span>
          </NavLink>
          <NavLink to="/login" className="btn-secondary small" title="Switch or Sign In to Another Account" style={{ padding: "4px 8px", fontSize: "12px", textDecoration: "none" }}>
            🔑 Sign In / Register
          </NavLink>
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

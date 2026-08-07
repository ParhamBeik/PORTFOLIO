import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes, useParams } from "react-router-dom";
import { logoutSession, me, restoreSession, SESSION_EXPIRED_EVENT, sessionExpiry } from "./api.js";
import Logo from "./components/Logo.jsx";
import Auth from "./components/Auth.jsx";
import { PortfolioProvider, usePortfolio } from "./components/PortfolioContext.jsx";

const Onboarding = lazy(() => import("./components/Onboarding.jsx"));
const Portfolio = lazy(() => import("./components/Portfolio.jsx"));
const MarketData = lazy(() => import("./components/MarketData.jsx"));
const OptimizationLayout = lazy(() => import("./components/OptimizationLayout.jsx"));
const Optimization = lazy(() => import("./components/Optimization.jsx"));
const BestOverall = lazy(() => import("./components/BestOverall.jsx"));
const Insights = lazy(() => import("./components/Insights.jsx"));
const Analytics = lazy(() => import("./components/Analytics.jsx"));
const TimeMachine = lazy(() => import("./components/TimeMachine.jsx"));
const Legal = lazy(() => import("./components/Legal.jsx"));
const Watchlist = lazy(() => import("./components/Watchlist.jsx"));

export default function App() {
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);
  const [startupError, setStartupError] = useState("");
  const [sessionNotice, setSessionNotice] = useState("");

  const loadUser = async () => {
    setReady(false);
    setStartupError("");
    try {
      await restoreSession();
      const profile = await me();
      setUser(profile);
    } catch {
      // No valid session — anonymous is a normal state here, not an error.
      setUser(null);
    } finally {
      setReady(true);
    }
  };

  useEffect(() => {
    loadUser();
  }, []);

  useEffect(() => {
    const onExpired = () => {
      setUser(null);
      setSessionNotice("Your session expired. Please sign in again.");
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired);
  }, []);

  if (!ready) return <div className="loading">Loading…</div>;

  if (startupError) {
    return (
      <div className="loading startup-error" role="alert">
        <p>We could not load the portfolio configuration: {startupError}</p>
        <button type="button" className="primary" onClick={loadUser}>Retry</button>
      </div>
    );
  }

  if (!user) {
    return (
      <>
        {sessionNotice && (
          <div className="error" role="alert" style={{ textAlign: "center" }}>
            {sessionNotice}
          </div>
        )}
        <Auth
          onAuthed={(profile) => {
            setSessionNotice("");
            setUser(profile);
          }}
        />
      </>
    );
  }

  return (
    <BrowserRouter>
      <PortfolioProvider key={user.id} enabled>
        <Suspense fallback={<div className="route-loading" role="status">Loading page…</div>}>
          <Routes>
            <Route path="/privacy" element={<Legal kind="privacy" />} />
            <Route path="/terms" element={<Legal kind="terms" />} />
            <Route element={<Shell user={user} onLogout={() => setUser(null)} />}>
              <Route path="/" element={<RequireOnboarding><Portfolio user={user} /></RequireOnboarding>} />
              <Route path="/onboarding" element={<Onboarding />} />
              <Route path="/market" element={<MarketData user={user} />} />
              <Route path="/watchlist" element={<Watchlist user={user} />} />
              <Route path="/accounts/:id" element={<AccountRedirect />} />
              <Route path="/dashboard" element={<Navigate to="/" replace />} />
              <Route path="/optimization" element={<OptimizationLayout user={user} />}>
                <Route index element={<Optimization user={user} />} />
                <Route path="best-overall" element={<BestOverall user={user} />} />
                <Route path="timemachine" element={<TimeMachine user={user} />} />
                <Route path="insights" element={<Insights user={user} />} />
                <Route path="analytics" element={<Analytics user={user} />} />
              </Route>
              <Route path="/insights" element={<Navigate to="/optimization/insights" replace />} />
              <Route path="/analytics" element={<Navigate to="/optimization/analytics" replace />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </Suspense>
      </PortfolioProvider>
    </BrowserRouter>
  );
}

// A brand-new user has nothing to render a dashboard from until they enter at
// least one holding. Only gate once the account list has actually loaded —
// otherwise a slow request looks like "no holdings" and bounces an existing
// user off their own dashboard.
function RequireOnboarding({ children }) {
  const { accounts, loading, error } = usePortfolio();
  if (loading || error) return children;
  const hasHoldings = accounts.some((a) => (a.holdings || []).length > 0);
  return hasHoldings ? children : <Navigate to="/onboarding" replace />;
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

function Shell({ user, onLogout }) {
  const { accounts, activeId, setActive, reload, loading, error, basis, setBasis } = usePortfolio();
  const [theme, setTheme] = useState(() => localStorage.getItem("theme") || "dark");

  const doLogout = async () => {
    await logoutSession();
    onLogout();
  };

  const daysLeft = (() => {
    if (!sessionExpiry.value) return null;
    const ms = new Date(sessionExpiry.value).getTime() - Date.now();
    return ms > 0 ? Math.ceil(ms / 86400000) : 0;
  })();

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);

  const toggleTheme = () => setTheme((t) => (t === "dark" ? "light" : "dark"));

  return (
    <div className="app">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <header className="topbar">
        <NavLink to="/" className="brand" aria-label="Lattice portfolio home">
          <Logo size={22} />
          <span className="brand-name">Lattice</span>
        </NavLink>
        <label className="sr-only" htmlFor="portfolio-scope">Active portfolio</label>
        <select
          id="portfolio-scope"
          className="portfolio-select"
          disabled={loading}
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
        <select
          id="basis-scope"
          className="portfolio-select"
          value={basis}
          onChange={(e) => setBasis(e.target.value)}
          style={{ marginLeft: "0.5rem" }}
        >
          <option value="nominal_toman">Nominal Toman</option>
          <option value="usd_denominated">USD-denominated</option>
          <option value="usdt_denominated">USDT-denominated</option>
        </select>
        <nav className="tabs" aria-label="Primary navigation">
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            Portfolio
          </NavLink>
          <NavLink to="/market" className={({ isActive }) => (isActive ? "active" : "")}>
            Market
          </NavLink>
          <NavLink to="/watchlist" className={({ isActive }) => (isActive ? "active" : "")}>
            Watchlist
          </NavLink>
          <NavLink to="/optimization" className={({ isActive }) => (isActive ? "active" : "")}>
            Optimization
          </NavLink>
          {user?.is_staff && (
            <a href="/admin/">
              Admin
            </a>
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
          <span className="email" title={daysLeft != null ? `Session ends in ${daysLeft} day${daysLeft === 1 ? "" : "s"}` : undefined}>
            {user.email}
          </span>
          <button type="button" onClick={doLogout}>Logout</button>
        </div>
      </header>
      <main id="main-content" tabIndex="-1">
        {error && (
          <div className="error" role="alert">
            Could not load portfolios: {error}
            <button type="button" className="link" onClick={reload}>Retry</button>
          </div>
        )}
        <Outlet />
      </main>
      <footer className="app-footer">
        <NavLink to="/privacy">Privacy</NavLink>
        <NavLink to="/terms">Terms</NavLink>
        <span>Informational use only.</span>
      </footer>
    </div>
  );
}

import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { auth, logoutSession, me, restoreSession, SESSION_EXPIRED_EVENT } from "./api.js";
import Logo from "./components/Logo.jsx";
import { ResetPassword, VerifyEmail } from "./components/AccountActions.jsx";
import { PortfolioProvider, usePortfolio } from "./components/PortfolioContext.jsx";

const Auth = lazy(() => import("./components/Auth.jsx"));
const Portfolio = lazy(() => import("./components/Portfolio.jsx"));
const Billing = lazy(() => import("./components/Billing.jsx"));
const MarketData = lazy(() => import("./components/MarketData.jsx"));
const OptimizationLayout = lazy(() => import("./components/OptimizationLayout.jsx"));
const Optimization = lazy(() => import("./components/Optimization.jsx"));
const Insights = lazy(() => import("./components/Insights.jsx"));
const Analytics = lazy(() => import("./components/Analytics.jsx"));
const Profile = lazy(() => import("./components/Profile.jsx"));
const TimeMachine = lazy(() => import("./components/TimeMachine.jsx"));
const Discovery = lazy(() => import("./components/Discovery.jsx"));
const Onboarding = lazy(() => import("./components/Onboarding.jsx"));
const Legal = lazy(() => import("./components/Legal.jsx"));
const Landing = lazy(() => import("./components/Landing.jsx"));
const Pricing = lazy(() => import("./components/Pricing.jsx"));
const Watchlist = lazy(() => import("./components/Watchlist.jsx"));
const Settings = lazy(() => import("./components/Settings.jsx"));

export default function App() {
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);
  const [startupError, setStartupError] = useState("");

  const loadUser = async () => {
    setReady(false);
    setStartupError("");
    try {
      if (!auth.token && !await restoreSession()) return;
      setUser(await me());
    } catch (error) {
      if (auth.token) setStartupError(error.message || "Could not load your account.");
    } finally {
      setReady(true);
    }
  };

  useEffect(() => {
    loadUser();
  }, []);

  useEffect(() => {
    const handleSessionExpired = () => setUser(null);
    window.addEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
  }, []);

  if (!ready) return <div className="loading">Loading…</div>;
  if (startupError && auth.token) {
    return (
      <div className="loading startup-error" role="alert">
        <p>We could not load your account: {startupError}</p>
        <div className="inline">
          <button type="button" className="primary" onClick={loadUser}>Retry</button>
          <button
            type="button"
            onClick={async () => {
              await logoutSession();
              setUser(null);
              setStartupError("");
            }}
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  // The shell (topbar + <Outlet/>) only renders behind auth; /login is standalone
  // so the Zarinpal redirect and unauthed deep links both land cleanly.
  return (
    <BrowserRouter>
      <PortfolioProvider key={user?.id ?? "anonymous"} enabled={Boolean(user)}>
        <Suspense fallback={<div className="route-loading" role="status">Loading page…</div>}>
          <Routes>
            <Route
              path="/login"
              element={<Auth initialMode="login" onAuthed={setUser} currentUser={user} />}
            />
            <Route path="/verify-email" element={<VerifyEmail />} />
            <Route path="/reset-password" element={<ResetPassword />} />
            <Route path="/privacy" element={<Legal kind="privacy" />} />
            <Route path="/terms" element={<Legal kind="terms" />} />
            <Route
              path="/register"
              element={<Auth initialMode="register" onAuthed={setUser} currentUser={user} />}
            />
            <Route
              path="/signup"
              element={<Auth initialMode="register" onAuthed={setUser} currentUser={user} />}
            />
            <Route element={user ? <Shell user={user} setUser={setUser} /> : <Outlet />}>
              <Route path="/" element={user ? <RequirePortfolio><Portfolio user={user} /></RequirePortfolio> : <Landing />} />
              <Route path="/pricing" element={<Pricing />} />
              <Route path="/onboarding" element={user ? <Onboarding /> : <Navigate to="/login" replace />} />
              <Route path="/market" element={user ? <MarketData user={user} /> : <Navigate to="/login" replace />} />
              <Route path="/watchlist" element={user ? <Watchlist user={user} /> : <Navigate to="/login" replace />} />
              <Route path="/billing" element={user ? <Billing user={user} setUser={setUser} /> : <Navigate to="/login" replace />} />
              <Route path="/profile" element={user ? <Profile user={user} setUser={setUser} /> : <Navigate to="/login" replace />} />
              <Route path="/settings" element={user ? <Settings user={user} setUser={setUser} /> : <Navigate to="/login" replace />} />
              <Route path="/accounts/:id" element={user ? <AccountRedirect /> : <Navigate to="/login" replace />} />
              <Route path="/dashboard" element={<Navigate to="/" replace />} />
              <Route path="/optimization" element={user ? <OptimizationLayout user={user} /> : <Navigate to="/login" replace />}>
                <Route index element={<Optimization user={user} />} />
                <Route path="timemachine" element={<TimeMachine user={user} />} />
                <Route path="discovery" element={<Discovery user={user} />} />
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

// A brand-new user has nothing to render a dashboard from, so send them to
// onboarding instead of an empty page with a scattering of forms. Only once the
// portfolio list has actually loaded — otherwise a slow request looks like
// "no portfolios" and bounces an existing user out of their own dashboard.
function RequirePortfolio({ children }) {
  const { accounts, loading, error } = usePortfolio();
  if (loading || error) return children;
  return accounts.length ? children : <Navigate to="/onboarding" replace />;
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
  const { accounts, activeId, setActive, reload, loading, error, basis, setBasis } = usePortfolio();
  const [theme, setTheme] = useState(() => localStorage.getItem("theme") || "dark");

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
        {/* The "slider at the top": pick which portfolio the whole page scopes to.
            null = "All portfolios" (the aggregate across every account). */}
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
          <NavLink to="/billing" className={({ isActive }) => (isActive ? "active" : "")}>
            Billing
          </NavLink>
          <NavLink to="/profile" className={({ isActive }) => (isActive ? "active" : "")}>
            Profile
          </NavLink>
          <NavLink to="/settings" className={({ isActive }) => (isActive ? "active" : "")}>
            Settings
          </NavLink>
          {user.is_staff && (
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
          <NavLink to="/profile" className="email-link" title="Account & Security Settings">
            <span className="email">{user.email}</span>
          </NavLink>
          <NavLink to="/login" className="btn-secondary small switch-account-link" title="Switch to another account">
            Switch account
          </NavLink>
          <button
            onClick={async () => {
              await logoutSession();
              setUser(null);
              navigate("/login");
            }}
          >
            Logout
          </button>
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

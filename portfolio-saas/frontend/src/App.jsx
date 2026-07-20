import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { auth, me } from "./api.js";
import Auth from "./components/Auth.jsx";
import Dashboard from "./components/Dashboard.jsx";
import Insights from "./components/Insights.jsx";
import Analytics from "./components/Analytics.jsx";
import Optimization from "./components/Optimization.jsx";
import Billing from "./components/Billing.jsx";
import AccountDetail from "./components/AccountDetail.jsx";
import MarketData from "./components/MarketData.jsx";

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
      <Routes>
        <Route
          path="/login"
          element={user ? <Navigate to="/dashboard" replace /> : <Auth onAuthed={setUser} />}
        />
        <Route element={user ? <Shell user={user} setUser={setUser} /> : <Navigate to="/login" replace />}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="accounts/:id" element={<AccountDetail user={user} />} />
          <Route path="market" element={<MarketData user={user} />} />
          <Route path="insights" element={<Insights user={user} setUser={setUser} />} />
          <Route path="analytics" element={<Analytics user={user} />} />
          <Route path="optimization" element={<Optimization user={user} />} />
          <Route path="billing" element={<Billing user={user} setUser={setUser} />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

function Shell({ user, setUser }) {
  const navigate = useNavigate();
  const locked = (label) => (user.is_pro ? label : `${label} 🔒`);
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">📊 Portfolio SaaS</div>
        <nav className="tabs">
          <NavLink to="/dashboard" className={({ isActive }) => (isActive ? "active" : "")}>
            Portfolio
          </NavLink>
          <NavLink to="/market" className={({ isActive }) => (isActive ? "active" : "")}>
            Market
          </NavLink>
          <NavLink to="/analytics" className={({ isActive }) => (isActive ? "active" : "")}>
            {locked("Analytics")}
          </NavLink>
          <NavLink to="/optimization" className={({ isActive }) => (isActive ? "active" : "")}>
            {locked("Optimize")}
          </NavLink>
          <NavLink to="/insights" className={({ isActive }) => (isActive ? "active" : "")}>
            {locked("Insights")}
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

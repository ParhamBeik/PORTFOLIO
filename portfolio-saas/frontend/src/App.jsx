import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { me, restoreSession, SESSION_EXPIRED_EVENT } from "./api.js";
import Auth from "./components/Auth.jsx";
import Legal from "./components/Legal.jsx";
import { PortfolioProvider, usePortfolio } from "./components/PortfolioContext.jsx";
import Shell from "./components/Shell.jsx";
import { Loading } from "./components/ui.jsx";

const Dashboard = lazy(() => import("./pages/Dashboard.jsx"));
const MyOptimal = lazy(() => import("./pages/MyOptimal.jsx"));
const BestOverall = lazy(() => import("./pages/BestOverall.jsx"));
const Onboarding = lazy(() => import("./pages/Onboarding.jsx"));
const Ops = lazy(() => import("./pages/Ops.jsx"));
const Ledger = lazy(() => import("./pages/Ledger.jsx"));
const Family = lazy(() => import("./pages/Family.jsx"));

export default function App() {
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    (async () => {
      try {
        await restoreSession();
        setUser(await me());
      } catch {
        setUser(null); // Anonymous is a normal state, not an error.
      } finally {
        setReady(true);
      }
    })();
  }, []);

  useEffect(() => {
    const onExpired = () => {
      setUser(null);
      setNotice("Your session expired. Please sign in again.");
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired);
  }, []);

  if (!ready) return <Loading testId="app-boot" />;

  if (!user) {
    return (
      <BrowserRouter>
        {notice && (
          <p role="alert" className="p-3 text-center text-sm text-[var(--c-warn)]">
            {notice}
          </p>
        )}
        <Routes>
          <Route
            path="*"
            element={
              <Auth
                onAuthed={(profile) => {
                  setNotice("");
                  setUser(profile);
                }}
              />
            }
          />
        </Routes>
      </BrowserRouter>
    );
  }

  return (
    <BrowserRouter>
      <PortfolioProvider key={user.id} enabled>
        <Suspense fallback={<Loading testId="route-loading" />}>
          <Routes>
            <Route path="/privacy" element={<Legal kind="privacy" />} />
            <Route path="/terms" element={<Legal kind="terms" />} />
              <Route element={<Shell user={user} onLogout={() => setUser(null)} />}>
              <Route index element={<RequireHoldings><Dashboard user={user} /></RequireHoldings>} />
              <Route path="/optimal" element={<MyOptimal />} />
              <Route path="/universe" element={<BestOverall />} />
              <Route path="/onboarding" element={<Onboarding />} />
              <Route path="/ledger" element={<Ledger />} />
              <Route path="/family" element={<Family />} />
              <Route path="/ops" element={<Ops user={user} />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </Suspense>
      </PortfolioProvider>
    </BrowserRouter>
  );
}

// A brand-new user has nothing to render a dashboard from. Only redirect once
// the account list has actually loaded — otherwise a slow request looks like
// "no holdings" and bounces an existing user off their own dashboard.
function RequireHoldings({ children }) {
  const { accounts, loading, error } = usePortfolio();
  if (loading || error) return children;
  return accounts.some((a) => a.holdings?.length) ? children : <Navigate to="/onboarding" replace />;
}

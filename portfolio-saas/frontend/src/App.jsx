import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { me, restoreSession, SESSION_EXPIRED_EVENT } from "./api.js";
import Auth from "./components/Auth.jsx";
import ResetPassword from "./components/ResetPassword.jsx";
import Legal from "./components/Legal.jsx";
import { PortfolioProvider, usePortfolio } from "./components/PortfolioContext.jsx";
import { hasAnyHoldings } from "./holdingsGate.js";
import Shell from "./components/Shell.jsx";
import { Loading } from "./components/ui.jsx";

const Dashboard = lazy(() => import("./pages/Dashboard.jsx"));
const MyOptimal = lazy(() => import("./pages/MyOptimal.jsx"));
const BestOverall = lazy(() => import("./pages/BestOverall.jsx"));
const Onboarding = lazy(() => import("./pages/Onboarding.jsx"));
const Ops = lazy(() => import("./pages/Ops.jsx"));
const Ledger = lazy(() => import("./pages/Ledger.jsx"));
const Family = lazy(() => import("./pages/Family.jsx"));
const Comparison = lazy(() => import("./pages/Comparison.jsx"));
const AssetHistory = lazy(() => import("./pages/AssetHistory.jsx"));

export default function App() {
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    (async () => {
      try {
        // `restoreSession` RESOLVES with null when there is no session rather
        // than throwing, so calling `me()` regardless sent a request that could
        // only 401 -- a second red line in the console of every signed-out
        // visitor. The refresh attempt itself is unavoidable: the cookie is
        // httpOnly, so asking is the only way to find out.
        const token = await restoreSession();
        setUser(token ? await me() : null);
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
          <p role="alert" className="p-3 text-center text-sm text-[var(--c-warn-text)]">
            {notice}
          </p>
        )}
        <Routes>
          {/* A privacy policy nobody can read without an account is not a
              privacy policy. The signed-out tree was a single catch-all, so
              /privacy and /terms both answered with the sign-in form -- which
              is also why `Legal`'s signed-out branch, and its "Back to sign in"
              link, had never once rendered. */}
          <Route path="/privacy" element={<Legal kind="privacy" />} />
          <Route path="/terms" element={<Legal kind="terms" />} />
          <Route path="/reset-password" element={<ResetPassword />} />
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
            <Route path="/reset-password" element={<ResetPassword />} />
            <Route
              element={
                <Shell
                  user={user}
                  onLogout={() => setUser(null)}
                  onUserChange={setUser}
                />
              }
            >
              {/* Inside the shell: these are reached from the footer of every
                  page, and rendering them outside it dropped a signed-in reader
                  onto a bare page whose only way out said "Back to sign in". */}
              <Route path="/privacy" element={<Legal kind="privacy" authed />} />
              <Route path="/terms" element={<Legal kind="terms" authed />} />
              <Route element={<HoldingsGate user={user} />}>
                <Route index element={<Dashboard user={user} />} />
                <Route path="/optimal" element={<MyOptimal />} />
                <Route path="/universe" element={<BestOverall />} />
                <Route path="/best-overall" element={<Navigate to="/universe" replace />} />
                <Route path="/onboarding" element={<Onboarding />} />
                <Route path="/ledger" element={<Ledger />} />
                <Route path="/family" element={<Family />} />
                <Route path="/breakdown" element={<Navigate to="/family" replace />} />
                <Route path="/comparison" element={<Comparison />} />
                <Route path="/prices" element={<AssetHistory />} />
                <Route path="/ops" element={<Ops user={user} />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Route>
            </Route>
          </Routes>
        </Suspense>
      </PortfolioProvider>
    </BrowserRouter>
  );
}

export { hasAnyHoldings } from "./holdingsGate.js";

// One gate for both directions: empty accounts belong on onboarding, and an
// account that already has holdings should not sit on "Add your first holding"
// because they bookmarked the URL or signed in from a deep link. The old guard
// lived only on `/`, so signing in at `/ledger` skipped onboarding entirely.
function HoldingsGate({ user }) {
  const { pathname } = useLocation();
  const { accounts, loading, error } = usePortfolio();
  const onOnboarding = pathname === "/onboarding";
  const exempt =
    pathname === "/privacy" ||
    pathname === "/terms" ||
    (pathname === "/ops" && user?.is_staff);

  if (loading || exempt) return <Outlet />;

  const hasHoldings = hasAnyHoldings(accounts);

  if (!error) {
    if (!hasHoldings && !onOnboarding) {
      return <Navigate to="/onboarding" replace />;
    }
    if (hasHoldings && onOnboarding) {
      return <Navigate to="/" replace />;
    }
  }

  return <Outlet />;
}

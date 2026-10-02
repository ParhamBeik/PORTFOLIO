import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { auth, me, restoreSession, SESSION_EXPIRED_EVENT } from "./api.js";
import { clearMobileData, hasOfflineSnapshot, isNative } from "./mobile.js";
import { Network } from "@capacitor/network";
import OfflinePortfolio from "./OfflinePortfolio.jsx";
import Auth from "./components/Auth.jsx";
import ResetPassword from "./components/ResetPassword.jsx";
import Legal from "./components/Legal.jsx";
import { PortfolioProvider, usePortfolio } from "./components/PortfolioContext.jsx";
import { hasAnyHoldings } from "./holdingsGate.js";
import Shell from "./components/Shell.jsx";
import { Loading } from "./components/ui.jsx";

const Onboarding = lazy(() => import("./pages/Onboarding.jsx"));
const Ops = lazy(() => import("./pages/Ops.jsx"));
const PortfolioDestination = lazy(() => import("./pages/Consolidated.jsx").then((m) => ({ default: m.PortfolioDestination })));
const ActivityDestination = lazy(() => import("./pages/Consolidated.jsx").then((m) => ({ default: m.ActivityDestination })));
const ResearchDestination = lazy(() => import("./pages/Consolidated.jsx").then((m) => ({ default: m.ResearchDestination })));
const CompareDestination = lazy(() => import("./pages/Consolidated.jsx").then((m) => ({ default: m.CompareDestination })));
const GuidanceDestination = lazy(() => import("./pages/Consolidated.jsx").then((m) => ({ default: m.GuidanceDestination })));

const PAGE_TITLES = {
  "/": "Portfolio",
  "/activity": "Activity",
  "/research": "Research",
  "/compare": "Compare",
  "/risk": "Risk",
  "/ledger": "Ledger",
  "/family": "Breakdown",
  "/breakdown": "Breakdown",
  "/comparison": "Comparison",
  "/prices": "Price history",
  "/optimal": "My Optimal",
  "/universe": "Best Overall",
  "/best-overall": "Best Overall",
  "/ops": "Operations",
  "/onboarding": "Onboarding",
  "/privacy": "Privacy",
  "/terms": "Terms",
  "/reset-password": "Reset password",
  "/login": "Sign in",
  "/signup": "Create account",
};

// Where to send someone after they sign in. Only a same-origin absolute path is
// accepted: `next` arrives in a URL anyone can hand out, so `//evil.com` (a
// protocol-relative URL) and a backslash variant (browsers normalise `\` to `/`)
// are open-redirect payloads, not paths. Anything else falls back to the root.
function safeNext(search) {
  const raw = new URLSearchParams(search).get("next");
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) {
    return "/";
  }
  return raw;
}

// The signed-out catch-all. Sends an unauthenticated deep link to /login while
// remembering where it was going, so a shared link to /ledger survives the
// detour instead of silently becoming "/".
function LoginRedirect() {
  const { pathname, search, hash } = useLocation();
  const attempted = `${pathname}${search}${hash}`;
  return (
    <Navigate
      replace
      to={
        pathname === "/"
          ? "/login"
          : `/login?next=${encodeURIComponent(attempted)}`
      }
    />
  );
}

// Markets was two unrelated tools under one tab; they now live where the plan
// puts them -- price history under Research, comparison as its own tab.
function MarketsRedirect() {
  const { search } = useLocation();
  const view = new URLSearchParams(search).get("view");
  return <Navigate to={view === "comparison" ? "/compare" : "/research?view=prices"} replace />;
}

function LegacyRedirect({ to }) {
  const { search, hash } = useLocation();
  const [path, targetQuery = ""] = to.split("?");
  const params = new URLSearchParams(search);
  for (const [key, value] of new URLSearchParams(targetQuery)) params.set(key, value);
  const query = params.toString();
  return <Navigate to={`${path}${query ? `?${query}` : ""}${hash}`} replace />;
}

// Signing in swaps one BrowserRouter for another, and the new one reads whatever
// URL the address bar holds -- which is still /login. Rewriting the URL before
// the state change is what makes the signed-in tree mount on the requested page;
// `HoldingsGate` still overrides it for an account with no holdings, which is the
// intended first-run path.
function AuthRoute({ mode, onAuthed }) {
  const { search } = useLocation();
  return (
    <Auth
      initialMode={mode}
      onAuthed={(profile) => {
        const next = safeNext(search);
        if (next !== "/login") window.history.replaceState(null, "", next);
        onAuthed(profile);
      }}
    />
  );
}

function RouteTitle({ signedIn = false }) {
  const { pathname } = useLocation();

  useEffect(() => {
    const page = PAGE_TITLES[pathname] || (signedIn ? "Portfolio" : "Sign in");
    document.title = `${page} — Holdings`;
  }, [pathname, signedIn]);

  return null;
}

export default function App() {
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState("");
  const [offline, setOffline] = useState(false);
  const [offlineAvailable, setOfflineAvailable] = useState(false);

  useEffect(() => {
    if (!isNative) return undefined;
    let live = true;
    const onConnection = async (event) => {
      if (event.detail) return;
      const available = await hasOfflineSnapshot().catch(() => false);
      if (!live) return;
      setOfflineAvailable(available);
      setOffline(true);
    };
    window.addEventListener("holdings:network-change", onConnection);
    return () => {
      live = false;
      window.removeEventListener("holdings:network-change", onConnection);
    };
  }, []);

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
        if (isNative) {
          const connected = await Network.getStatus().then((s) => s.connected).catch(() => true);
          if (!connected) {
            setOfflineAvailable(await hasOfflineSnapshot().catch(() => false));
            setOffline(true);
          }
        }
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

  if (offline) {
    return <OfflinePortfolio available={offlineAvailable} onSignOut={async () => {
      auth.logout();
      await clearMobileData();
      setUser(null);
      setOffline(false);
    }} onReconnect={async () => {
      try {
        const token = await restoreSession();
        if (!token) {
          setOffline(false);
          setNotice("Your session expired. Please sign in again.");
          return;
        }
        setUser(await me());
        setOffline(false);
      } catch {
        throw new Error("Still offline. Your saved portfolio remains available.");
      }
    }} />;
  }

  const onAuthed = (profile) => {
    setNotice("");
    setUser(profile);
  };

  if (!user) {
    return (
      <BrowserRouter>
        <RouteTitle />
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
          {/* Real routes, not just a catch-all: /signup is a URL you can hand
              someone, and `Legal`'s "Back to sign in" link targets /login,
              which previously resolved only by accident. */}
          <Route
            path="/login"
            element={<AuthRoute mode="login" onAuthed={onAuthed} />}
          />
          <Route
            path="/signup"
            element={<AuthRoute mode="signup" onAuthed={onAuthed} />}
          />
          <Route path="*" element={<LoginRedirect />} />
        </Routes>
      </BrowserRouter>
    );
  }

  return (
    <BrowserRouter>
      <RouteTitle signedIn />
      <PortfolioProvider key={user.id} enabled>
        <Suspense fallback={<Loading testId="route-loading" />}>
          <Routes>
            <Route path="/reset-password" element={<ResetPassword />} />
            {/* A signed-in visitor has no business on the auth card -- and one
                lands here whenever sign-in did not rewrite the URL. */}
            <Route path="/login" element={<Navigate to="/" replace />} />
            <Route path="/signup" element={<Navigate to="/" replace />} />
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
              <Route path="/research" element={<ResearchDestination />} />
              <Route path="/explore" element={<LegacyRedirect to="/research?view=companies" />} />
              <Route element={<HoldingsGate user={user} />}>
                <Route index element={<PortfolioDestination user={user} />} />
                <Route path="/activity" element={<ActivityDestination />} />
                <Route path="/compare" element={<CompareDestination />} />
                <Route path="/risk" element={<GuidanceDestination user={user} onUserChange={setUser} />} />
                <Route path="/markets" element={<MarketsRedirect />} />
                <Route path="/guidance" element={<LegacyRedirect to="/risk" />} />
                <Route path="/optimal" element={<LegacyRedirect to="/risk?view=personal" />} />
                <Route path="/universe" element={<LegacyRedirect to="/risk?view=benchmark" />} />
                <Route path="/best-overall" element={<LegacyRedirect to="/risk?view=benchmark" />} />
                <Route path="/onboarding" element={<Onboarding />} />
                <Route path="/ledger" element={<LegacyRedirect to="/activity" />} />
                <Route path="/family" element={<LegacyRedirect to="/?view=breakdown" />} />
                <Route path="/breakdown" element={<LegacyRedirect to="/?view=breakdown" />} />
                <Route path="/comparison" element={<LegacyRedirect to="/compare" />} />
                <Route path="/prices" element={<LegacyRedirect to="/research?view=prices" />} />
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
    (pathname === "/ops" && user?.role === "admin");

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

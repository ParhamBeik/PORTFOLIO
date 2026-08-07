import { useEffect, useRef, useState } from "react";
import {
  auth,
  confirmPasswordReset,
  googleLogin,
  login,
  me,
  register,
  requestPasswordReset,
  sessionExpiry,
} from "../api.js";
import Logo from "./Logo.jsx";
import { Button, Input } from "./ui.jsx";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const GOOGLE_SCRIPT_SRC = "https://accounts.google.com/gsi/client";

function getPasswordChecks(pw, confirmPw = "", registering = false) {
  const checks = [
    { id: "length", ok: pw.length >= 8, label: "At least 8 characters" },
    { id: "mixed", ok: /[a-z]/.test(pw) && /[A-Z]/.test(pw), label: "Uppercase & lowercase letters" },
    { id: "number_symbol", ok: /\d/.test(pw) || /[^\w\s]/.test(pw), label: "Number or symbol" },
    { id: "not_digits", ok: pw.length > 0 && !/^\d+$/.test(pw), label: "Not all numbers" },
  ];
  if (registering) {
    checks.push({
      id: "match",
      ok: Boolean(pw && confirmPw && pw === confirmPw),
      label: "Passwords match",
    });
  }
  return checks;
}

function calculateStrength(pw, checks) {
  if (!pw) return { score: 0, label: "", color: "transparent" };
  const passed = checks.filter((c) => c.id !== "match" && c.ok).length;
  if (passed <= 1) return { score: 25, label: "Weak", color: "var(--c-critical)" };
  if (passed === 2 || passed === 3) return { score: 65, label: "Medium", color: "var(--c-warn)" };
  return { score: 100, label: "Strong", color: "var(--c-good)" };
}

function friendlyError(msg) {
  const lower = (msg || "").toLowerCase();
  if (lower.includes("email") && (lower.includes("exists") || lower.includes("already"))) {
    return "An account with this email address already exists. Try signing in instead.";
  }
  if (lower.includes("no active account") || lower.includes("credentials") || lower.includes("incorrect")) {
    return "Invalid email address or password. Please check your credentials and try again.";
  }
  return msg || "An unexpected error occurred.";
}

// Google Identity Services renders its own button into a DOM node; there is no
// npm dependency, just this one script tag and a global callback.
function useGoogleButton(onCredential) {
  const boxRef = useRef(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const clientId = import.meta.env.VITE_GOOGLE_OAUTH_CLIENT_ID;
    if (!clientId) return;
    let cancelled = false;

    const render = () => {
      if (cancelled || !boxRef.current || !window.google?.accounts?.id) return;
      window.google.accounts.id.initialize({
        client_id: clientId,
        callback: (response) => onCredential(response.credential),
      });
      window.google.accounts.id.renderButton(boxRef.current, {
        theme: "outline",
        size: "large",
        width: 360,
        text: "continue_with",
      });
      setReady(true);
    };

    const existing = document.querySelector(`script[src="${GOOGLE_SCRIPT_SRC}"]`);
    if (existing && window.google?.accounts?.id) {
      render();
    } else {
      const script = existing || document.createElement("script");
      script.src = GOOGLE_SCRIPT_SRC;
      script.async = true;
      script.defer = true;
      script.addEventListener("load", render);
      if (!existing) document.head.appendChild(script);
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { boxRef, ready: ready && Boolean(import.meta.env.VITE_GOOGLE_OAUTH_CLIENT_ID) };
}

export default function Auth({ onAuthed }) {
  const params = new URLSearchParams(window.location.search);
  const resetUid = params.get("uid");
  const resetToken = params.get("token");
  const isResetLink = window.location.pathname === "/reset-password" && resetUid && resetToken;

  const [mode, setMode] = useState(isResetLink ? "reset-confirm" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [showConfirmPw, setShowConfirmPw] = useState(false);
  const [error, setError] = useState("");
  const [fieldError, setFieldError] = useState({});
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const registering = mode === "signup";
  const emailValid = EMAIL_RE.test(email.trim());
  const checks = getPasswordChecks(password, confirmPassword, registering);
  const pwValid = checks.every((c) => c.ok);
  const strength = calculateStrength(password, checks);

  const switchMode = (next) => {
    setMode(next);
    setError("");
    setFieldError({});
    setNotice("");
  };

  const finishAuth = async (data) => {
    auth.tokens = data;
    sessionExpiry.set(data.session_expires_at);
    onAuthed(await me());
  };

  const handleGoogleCredential = async (credential) => {
    setBusy(true);
    setError("");
    try {
      await finishAuth(await googleLogin(credential));
    } catch (err) {
      setError(friendlyError(err.message));
    } finally {
      setBusy(false);
    }
  };
  const { boxRef: googleBoxRef, ready: googleReady } = useGoogleButton(handleGoogleCredential);

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    setFieldError({});
    try {
      if (registering) {
        await finishAuth(await register(email.trim(), password));
      } else {
        await finishAuth(await login(email.trim(), password));
      }
    } catch (err) {
      const msg = err.message || "";
      if (msg.toLowerCase().includes("password") && !msg.toLowerCase().includes("incorrect")) {
        setFieldError({ password: msg });
      }
      setError(friendlyError(msg));
    } finally {
      setBusy(false);
    }
  }

  async function submitResetRequest(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await requestPasswordReset(email.trim());
      setNotice("If that account exists, a password reset email is on its way.");
    } catch (err) {
      setError(err.message || "Could not request a password reset.");
    } finally {
      setBusy(false);
    }
  }

  async function submitResetConfirm(e) {
    e.preventDefault();
    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await confirmPasswordReset(resetUid, resetToken, password, confirmPassword);
      setNotice("Password reset. You can now sign in with your new password.");
      window.history.replaceState({}, "", "/login");
      switchMode("login");
    } catch (err) {
      setError(err.message || "This reset link is invalid or expired.");
    } finally {
      setBusy(false);
    }
  }

  if (mode === "reset-confirm") {
    return (
      <div className="flex min-h-screen items-center justify-center px-4">
        <form
          data-testid="auth-card"
          className="w-full max-w-md rounded-xl border border-border bg-panel p-5"
          onSubmit={submitResetConfirm}
          noValidate
        >
          <div className="mb-6">
            <h1 className="flex items-center gap-2 text-xl font-semibold text-text">
              <Logo size={36} />
              <span>Lattice</span>
            </h1>
            <p className="mt-1 text-sm text-muted">Choose a new password.</p>
          </div>
          {error && (
            <div
              data-testid="auth-error-banner"
              role="alert"
              className="mb-4 rounded-lg border border-[var(--c-critical)]/40 bg-[var(--c-critical)]/10 px-4 py-3 text-sm text-[var(--c-critical)]"
            >
              {error}
            </div>
          )}
          <div className="mb-4">
            <label htmlFor="reset-new-password" className="mb-1 block text-sm font-medium text-text">
              New password
            </label>
            <Input
              id="reset-new-password"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full"
            />
          </div>
          <div className="mb-4">
            <label htmlFor="reset-confirm-password" className="mb-1 block text-sm font-medium text-text">
              Confirm password
            </label>
            <Input
              id="reset-confirm-password"
              type="password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="w-full"
            />
          </div>
          <Button type="submit" variant="primary" disabled={busy} className="w-full py-1.5">
            {busy ? "Saving…" : "Reset password"}
          </Button>
        </form>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        data-testid="auth-card"
        className="w-full max-w-md rounded-xl border border-border bg-panel p-5"
        onSubmit={mode === "reset" ? submitResetRequest : submit}
        noValidate
      >
        <div className="mb-6">
          <h1 className="flex items-center gap-2 text-xl font-semibold text-text">
            <Logo size={36} />
            <span>Lattice</span>
          </h1>
          <p className="mt-1 text-sm text-muted">
            {mode === "reset"
              ? "Enter your email and we'll send you a reset link."
              : registering
              ? "Create your account to start tracking multi-asset portfolios live."
              : "Welcome back! Sign in to access your portfolios and analytics."}
          </p>
        </div>

        {mode !== "reset" && (
          <div
            role="tablist"
            aria-label="Authentication mode"
            className="mb-4 inline-flex gap-1 rounded-lg border border-border bg-panel-2 p-1"
          >
            <button
              type="button"
              role="tab"
              aria-selected={mode === "login"}
              data-testid="auth-toggle-login"
              onClick={() => switchMode("login")}
              className={`rounded-md px-3 py-1 text-sm font-medium transition-colors ${
                mode === "login" ? "bg-accent text-white" : "text-muted hover:text-text"
              }`}
            >
              Sign in
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "signup"}
              data-testid="auth-toggle-register"
              onClick={() => switchMode("signup")}
              className={`rounded-md px-3 py-1 text-sm font-medium transition-colors ${
                mode === "signup" ? "bg-accent text-white" : "text-muted hover:text-text"
              }`}
            >
              Create account
            </button>
          </div>
        )}

        {error && (
          <div
            data-testid="auth-error-banner"
            role="alert"
            aria-live="polite"
            className="mb-4 rounded-lg border border-[var(--c-critical)]/40 bg-[var(--c-critical)]/10 px-4 py-3 text-sm text-[var(--c-critical)]"
          >
            {error}
          </div>
        )}
        {notice && (
          <div
            role="status"
            className="mb-4 rounded-lg border border-[var(--c-good)]/40 bg-[var(--c-good)]/10 px-4 py-3 text-sm text-[var(--c-good)]"
          >
            {notice}
          </div>
        )}

        <div className="mb-4">
          <div className="mb-1 flex items-center justify-between">
            <label htmlFor="auth-email" className="text-sm font-medium text-text">
              Email address
            </label>
            {email && (
              <span
                className={`text-xs font-medium ${
                  emailValid ? "text-[var(--c-good)]" : "text-[var(--c-critical)]"
                }`}
              >
                {emailValid ? "✓ Valid" : "Invalid"}
              </span>
            )}
          </div>
          <Input
            id="auth-email"
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            aria-invalid={Boolean(fieldError.email)}
            data-testid="auth-email-input"
            className="w-full"
          />
          {fieldError.email && (
            <p className="mt-1 text-xs text-[var(--c-critical)]">{fieldError.email}</p>
          )}
        </div>

        {mode !== "reset" && (
          <div className="mb-4">
            <div className="mb-1 flex items-center justify-between">
              <label htmlFor="auth-password" className="text-sm font-medium text-text">
                Password
              </label>
              {registering && strength.label && (
                <span className="text-xs font-medium" style={{ color: strength.color }}>
                  Strength: {strength.label}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Input
                id="auth-password"
                type={showPw ? "text" : "password"}
                required
                autoComplete={registering ? "new-password" : "current-password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={registering ? "Create a password" : "Enter password"}
                aria-invalid={Boolean(fieldError.password)}
                data-testid="auth-password-input"
                className={`w-full ${fieldError.password ? "border-[var(--c-critical)]" : ""}`}
              />
              <Button
                type="button"
                variant="ghost"
                onClick={() => setShowPw((v) => !v)}
                data-testid="auth-password-toggle"
                className="shrink-0 px-2.5 py-1.5 text-xs"
              >
                {showPw ? "Hide" : "Show"}
              </Button>
            </div>
            {fieldError.password && (
              <p className="mt-1 text-xs text-[var(--c-critical)]">{fieldError.password}</p>
            )}
            {registering && password && (
              <div data-testid="auth-strength-meter" className="mt-2 h-1 overflow-hidden rounded-full bg-panel-2">
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${strength.score}%`, backgroundColor: strength.color }}
                />
              </div>
            )}
          </div>
        )}

        {registering && (
          <div className="mb-4">
            <div className="mb-1 flex items-center justify-between">
              <label htmlFor="auth-confirm-password" className="text-sm font-medium text-text">
                Confirm password
              </label>
              {confirmPassword && (
                <span
                  className={`text-xs font-medium ${
                    password === confirmPassword ? "text-[var(--c-good)]" : "text-[var(--c-critical)]"
                  }`}
                >
                  {password === confirmPassword ? "✓ Match" : "Mismatch"}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Input
                id="auth-confirm-password"
                type={showConfirmPw ? "text" : "password"}
                required
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="Re-enter password"
                className="w-full"
              />
              <Button
                type="button"
                variant="ghost"
                onClick={() => setShowConfirmPw((v) => !v)}
                className="shrink-0 px-2.5 py-1.5 text-xs"
              >
                {showConfirmPw ? "Hide" : "Show"}
              </Button>
            </div>
          </div>
        )}

        {registering && (
          <div className="mb-4 rounded-lg border border-border bg-panel-2 px-4 py-3">
            <span className="text-sm font-medium text-text">Password requirements:</span>
            <ul className="mt-2 space-y-1 text-sm">
              {checks.map((c) => (
                <li
                  key={c.id}
                  className={`flex items-center gap-2 ${c.ok ? "text-[var(--c-good)]" : "text-muted"}`}
                >
                  <span>{c.ok ? "✓" : "○"}</span>
                  <span>{c.label}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <Button
          type="submit"
          variant="primary"
          data-testid="auth-submit"
          disabled={busy || (mode === "reset" ? !emailValid : registering ? !(emailValid && pwValid) : !(emailValid && password))}
          className="w-full py-1.5"
        >
          {busy
            ? "Working…"
            : mode === "reset"
            ? "Send reset link"
            : registering
            ? "Create account"
            : "Sign in"}
        </Button>

        {googleReady && (
          <div className="my-4 flex items-center gap-3 text-xs text-muted">
            <span className="h-px flex-1 bg-border" />
            <span>or</span>
            <span className="h-px flex-1 bg-border" />
          </div>
        )}
        <div ref={googleBoxRef} className="flex justify-center" />

        <div className="mt-4 flex justify-center text-sm">
          {mode !== "reset" ? (
            <Button type="button" variant="link" onClick={() => switchMode("reset")}>
              Forgot password?
            </Button>
          ) : (
            <Button type="button" variant="link" onClick={() => switchMode("login")}>
              Back to sign in
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}

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
  if (passed <= 1) return { score: 25, label: "Weak", color: "var(--red)" };
  if (passed === 2 || passed === 3) return { score: 65, label: "Medium", color: "var(--amber)" };
  return { score: 100, label: "Strong", color: "var(--green)" };
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
      <div className="auth-wrap">
        <div className="auth-card-wrapper">
          <form className="auth-card" onSubmit={submitResetConfirm} noValidate>
            <div className="auth-header">
              <h1 className="brand-heading"><Logo size={36} /><span>Lattice</span></h1>
              <p className="subtitle">Choose a new password.</p>
            </div>
            {error && <div className="error-banner" role="alert"><div className="error-text">{error}</div></div>}
            <div className="form-group">
              <label htmlFor="reset-new-password">New password</label>
              <input
                id="reset-new-password"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="form-group">
              <label htmlFor="reset-confirm-password">Confirm password</label>
              <input
                id="reset-confirm-password"
                type="password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
              />
            </div>
            <button type="submit" className="primary big submit-btn" disabled={busy}>
              {busy ? "Saving…" : "Reset password"}
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="auth-wrap">
      <div className="auth-card-wrapper">
        <form className="auth-card" onSubmit={mode === "reset" ? submitResetRequest : submit} noValidate>
          <div className="auth-header">
            <h1 className="brand-heading">
              <Logo size={36} />
              <span>Lattice</span>
            </h1>
            <p className="subtitle">
              {mode === "reset"
                ? "Enter your email and we'll send you a reset link."
                : registering
                ? "Create your account to start tracking multi-asset portfolios live."
                : "Welcome back! Sign in to access your portfolios and analytics."}
            </p>
          </div>

          {mode !== "reset" && (
            <div className="seg" role="tablist" aria-label="Authentication mode">
              <button
                type="button"
                role="tab"
                aria-selected={mode === "login"}
                className={mode === "login" ? "active" : ""}
                onClick={() => switchMode("login")}
              >
                Sign in
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === "signup"}
                className={mode === "signup" ? "active" : ""}
                onClick={() => switchMode("signup")}
              >
                Create account
              </button>
            </div>
          )}

          {error && (
            <div className="error-banner" role="alert" aria-live="polite">
              <div className="error-text">{error}</div>
            </div>
          )}
          {notice && <div className="alert-success" role="status">{notice}</div>}

          <div className="form-group">
            <div className="label-row">
              <label htmlFor="auth-email">Email address</label>
              {email && (
                <span className={`field-status ${emailValid ? "valid" : "invalid"}`}>
                  {emailValid ? "✓ Valid" : "Invalid"}
                </span>
              )}
            </div>
            <input
              id="auth-email"
              type="email"
              required
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              aria-invalid={Boolean(fieldError.email)}
            />
            {fieldError.email && <p className="field-hint error-hint">{fieldError.email}</p>}
          </div>

          {mode !== "reset" && (
            <div className="form-group">
              <div className="label-row">
                <label htmlFor="auth-password">Password</label>
                {registering && strength.label && (
                  <span className="strength-label" style={{ color: strength.color }}>
                    Strength: {strength.label}
                  </span>
                )}
              </div>
              <div className="pw-field">
                <input
                  id="auth-password"
                  type={showPw ? "text" : "password"}
                  required
                  autoComplete={registering ? "new-password" : "current-password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={registering ? "Create a password" : "Enter password"}
                  aria-invalid={Boolean(fieldError.password)}
                  className={fieldError.password ? "input-error" : ""}
                />
                <button type="button" className="pw-toggle" onClick={() => setShowPw((v) => !v)}>
                  {showPw ? "Hide" : "Show"}
                </button>
              </div>
              {fieldError.password && <p className="field-hint error-hint">{fieldError.password}</p>}
              {registering && password && (
                <div className="strength-bar-wrap">
                  <div className="strength-bar-fill" style={{ width: `${strength.score}%`, backgroundColor: strength.color }} />
                </div>
              )}
            </div>
          )}

          {registering && (
            <div className="form-group">
              <div className="label-row">
                <label htmlFor="auth-confirm-password">Confirm password</label>
                {confirmPassword && (
                  <span className={`field-status ${password === confirmPassword ? "valid" : "invalid"}`}>
                    {password === confirmPassword ? "✓ Match" : "Mismatch"}
                  </span>
                )}
              </div>
              <div className="pw-field">
                <input
                  id="auth-confirm-password"
                  type={showConfirmPw ? "text" : "password"}
                  required
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder="Re-enter password"
                />
                <button type="button" className="pw-toggle" onClick={() => setShowConfirmPw((v) => !v)}>
                  {showConfirmPw ? "Hide" : "Show"}
                </button>
              </div>
            </div>
          )}

          {registering && (
            <div className="pw-rules-card">
              <span className="rules-title">Password requirements:</span>
              <ul className="pw-rules">
                {checks.map((c) => (
                  <li key={c.id} className={c.ok ? "ok" : "muted"}>
                    <span className="rule-icon">{c.ok ? "✓" : "○"}</span>
                    <span>{c.label}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <button
            type="submit"
            className="primary big submit-btn"
            disabled={busy || (mode === "reset" ? !emailValid : registering ? !(emailValid && pwValid) : !(emailValid && password))}
          >
            {busy
              ? "Working…"
              : mode === "reset"
              ? "Send reset link"
              : registering
              ? "Create account"
              : "Sign in"}
          </button>

          {googleReady && (
            <div className="oauth-divider">
              <span>or</span>
            </div>
          )}
          <div ref={googleBoxRef} className="google-btn-box" />

          <div className="legal-links">
            {mode !== "reset" ? (
              <button type="button" className="link" onClick={() => switchMode("reset")}>
                Forgot password?
              </button>
            ) : (
              <button type="button" className="link" onClick={() => switchMode("login")}>
                Back to sign in
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}

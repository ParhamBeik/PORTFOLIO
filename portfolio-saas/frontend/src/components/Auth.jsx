import { useState, useEffect } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { auth, login, me, register } from "../api.js";
import Logo from "./Logo.jsx";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

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
  if (passed <= 1) return { score: 25, label: "Weak", color: "var(--danger, #ef4444)" };
  if (passed === 2 || passed === 3) return { score: 65, label: "Medium", color: "var(--amber, #f59e0b)" };
  return { score: 100, label: "Strong", color: "var(--green, #22c55e)" };
}

export default function Auth({ initialMode = "login", onAuthed, currentUser = null }) {
  const navigate = useNavigate();
  const location = useLocation();

  const isRegisterPath = location.pathname === "/register";
  const [mode, setMode] = useState(isRegisterPath || initialMode === "register" ? "register" : "login");

  useEffect(() => {
    if (location.pathname === "/register" && mode !== "register") {
      setMode("register");
    } else if (location.pathname === "/login" && mode !== "login") {
      setMode("login");
    }
  }, [location.pathname]);

  const switchMode = (newMode) => {
    setMode(newMode);
    setError("");
    setFieldError({});
    const targetPath = newMode === "register" ? "/register" : "/login";
    if (location.pathname !== targetPath) {
      navigate(targetPath, { replace: true });
    }
  };

  const [email, setEmail] = useState(() => localStorage.getItem("remembered_email") || "");
  const [rememberEmail, setRememberEmail] = useState(() => Boolean(localStorage.getItem("remembered_email")));
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");

  const [showPw, setShowPw] = useState(false);
  const [showConfirmPw, setShowConfirmPw] = useState(false);
  const [error, setError] = useState("");
  const [fieldError, setFieldError] = useState({});
  const [busy, setBusy] = useState(false);

  const registering = mode === "register";
  const emailValid = EMAIL_RE.test(email.trim());
  const checks = getPasswordChecks(password, confirmPassword, registering);
  const pwValid = checks.every((c) => c.ok);
  const strength = calculateStrength(password, checks);

  const canSubmit =
    !busy &&
    (registering
      ? emailValid && pwValid && firstName.trim().length > 0 && confirmPassword.length > 0
      : emailValid && password.length > 0);

  const handleDemoFill = () => {
    switchMode("login");
    setEmail("demo@portfolio.local");
    setPassword("demo12345");
    setError("");
    setFieldError({});
  };

  async function submit(e) {
    e.preventDefault();
    if (!canSubmit) return;

    setBusy(true);
    setError("");
    setFieldError({});

    if (rememberEmail && email) {
      localStorage.setItem("remembered_email", email.trim());
    } else {
      localStorage.removeItem("remembered_email");
    }

    try {
      let data;
      if (registering) {
        data = await register(email.trim(), password, firstName.trim(), lastName.trim());
      } else {
        data = await login(email.trim(), password);
      }
      auth.tokens = data;
      onAuthed(await me());
      navigate("/");
    } catch (err) {
      const msg = err.message || "An unexpected error occurred.";
      const lower = msg.toLowerCase();
      if (lower.includes("email") && (lower.includes("exists") || lower.includes("already"))) {
        setFieldError({ email: "An account with this email address already exists." });
        setError("Account already exists. Please sign in instead.");
      } else if (lower.includes("no active account") || lower.includes("credentials") || lower.includes("incorrect")) {
        setError("Invalid email address or password. Please check your credentials and try again.");
      } else if (lower.includes("password")) {
        setFieldError({ password: msg });
        setError("Your password does not meet security requirements.");
      } else {
        setError(msg);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <div className="auth-card-wrapper">
        <form className="auth-card" onSubmit={submit} noValidate>
          <div className="auth-header">
            <h1 className="brand-heading">
              <Logo size={36} />
              <span>Lattice</span>
            </h1>
            <p className="subtitle">
              {registering
                ? "Create your account to start tracking multi-asset portfolios live in Tomans."
                : "Welcome back! Sign in to access your portfolios, market analytics, and pro tools."}
            </p>
          </div>

          {currentUser && (
            <div style={{ padding: "10px 14px", background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: "8px", marginBottom: "16px", fontSize: "13px" }}>
              ℹ️ Signed in as <strong>{currentUser.email}</strong>. You can sign in to another account below or{" "}
              <button
                type="button"
                style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", textDecoration: "underline", padding: 0, font: "inherit" }}
                onClick={() => navigate("/")}
              >
                return to Dashboard
              </button>.
            </div>
          )}

          <div className="seg" role="tablist" aria-label="Authentication Options">
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
              aria-selected={mode === "register"}
              className={mode === "register" ? "active" : ""}
              onClick={() => switchMode("register")}
            >
              Create account
            </button>
          </div>

          {error && (
            <div className="error-banner" role="alert" aria-live="polite">
              <span className="error-icon">⚠️</span>
              <div className="error-text">{error}</div>
            </div>
          )}

          {registering && (
            <div className="form-row">
              <div className="form-group">
                <label htmlFor="auth-first-name">First Name</label>
                <input
                  id="auth-first-name"
                  type="text"
                  required
                  autoComplete="given-name"
                  value={firstName}
                  onChange={(e) => setFirstName(e.target.value)}
                  placeholder="e.g. John"
                  className={firstName.trim() ? "input-valid" : ""}
                />
              </div>
              <div className="form-group">
                <label htmlFor="auth-last-name">Last Name</label>
                <input
                  id="auth-last-name"
                  type="text"
                  autoComplete="family-name"
                  value={lastName}
                  onChange={(e) => setLastName(e.target.value)}
                  placeholder="e.g. Doe"
                />
              </div>
            </div>
          )}

          <div className="form-group">
            <div className="label-row">
              <label htmlFor="auth-email">Email Address</label>
              {email && (
                <span className={`field-status ${emailValid ? "valid" : "invalid"}`}>
                  {emailValid ? "✓ Valid email" : "Invalid email"}
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
              aria-invalid={Boolean(email && !emailValid) || Boolean(fieldError.email)}
              aria-describedby={fieldError.email ? "email-error-hint" : undefined}
              className={fieldError.email ? "input-error" : emailValid ? "input-valid" : ""}
            />
            {fieldError.email ? (
              <p id="email-error-hint" className="field-hint error-hint">
                {fieldError.email}
              </p>
            ) : registering && email && !emailValid ? (
              <p className="field-hint warning-hint">Please enter a valid email address (e.g. name@domain.com).</p>
            ) : null}
          </div>

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
                placeholder={registering ? "Create strong password" : "Enter password"}
                aria-invalid={Boolean(fieldError.password)}
                className={fieldError.password ? "input-error" : ""}
              />
              <button
                type="button"
                className="pw-toggle"
                onClick={() => setShowPw((v) => !v)}
                aria-label={showPw ? "Hide password" : "Show password"}
              >
                {showPw ? "Hide" : "Show"}
              </button>
            </div>
            {fieldError.password && <p className="field-hint error-hint">{fieldError.password}</p>}

            {registering && password && (
              <div className="strength-bar-wrap">
                <div
                  className="strength-bar-fill"
                  style={{ width: `${strength.score}%`, backgroundColor: strength.color }}
                />
              </div>
            )}
          </div>

          {registering && (
            <div className="form-group">
              <div className="label-row">
                <label htmlFor="auth-confirm-password">Confirm Password</label>
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
                  aria-invalid={Boolean(confirmPassword && password !== confirmPassword)}
                  className={confirmPassword && password !== confirmPassword ? "input-error" : confirmPassword && password === confirmPassword ? "input-valid" : ""}
                />
                <button
                  type="button"
                  className="pw-toggle"
                  onClick={() => setShowConfirmPw((v) => !v)}
                  aria-label={showConfirmPw ? "Hide confirm password" : "Show confirm password"}
                >
                  {showConfirmPw ? "Hide" : "Show"}
                </button>
              </div>
              {confirmPassword && password !== confirmPassword && (
                <p className="field-hint error-hint">Passwords do not match.</p>
              )}
            </div>
          )}

          {registering && (
            <div className="pw-rules-card">
              <span className="rules-title">Password Requirements:</span>
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

          {!registering && (
            <div className="auth-options">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={rememberEmail}
                  onChange={(e) => setRememberEmail(e.target.checked)}
                />
                <span>Remember my email</span>
              </label>
            </div>
          )}

          <button type="submit" className="primary big submit-btn" disabled={!canSubmit}>
            {busy ? (
              <span className="btn-spinner-wrap">
                <span className="btn-spinner" aria-hidden="true" />
                <span>{registering ? "Creating account…" : "Signing in…"}</span>
              </span>
            ) : registering ? (
              "Create Account"
            ) : (
              "Sign In"
            )}
          </button>

          {!registering && (
            <div className="demo-box">
              <div className="demo-header">
                <span>Quick Access:</span>
                <button type="button" className="demo-btn" onClick={handleDemoFill}>
                  ⚡ Autofill Demo Credentials
                </button>
              </div>
              <p className="demo-hint">
                Demo email: <code>demo@portfolio.local</code> | password: <code>demo12345</code>
              </p>
            </div>
          )}
        </form>
      </div>
    </div>
  );
}

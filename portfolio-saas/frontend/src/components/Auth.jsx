import { useState } from "react";
import { auth, login, me, register } from "../api.js";
import Logo from "./Logo.jsx";

// Client-side mirror of the backend AUTH_PASSWORD_VALIDATORS so the register
// form can give live feedback; the backend still enforces the real rules.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
function passwordChecks(pw) {
  return [
    { ok: pw.length >= 8, label: "At least 8 characters" },
    { ok: !/^\d+$/.test(pw), label: "Not all numbers" },
  ];
}

export default function Auth({ onAuthed }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showPw, setShowPw] = useState(false);

  const registering = mode === "register";
  const emailValid = EMAIL_RE.test(email);
  const checks = passwordChecks(password);
  const pwValid = checks.every((c) => c.ok);
  const canSubmit = !busy && (!registering || (emailValid && pwValid));

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = registering
        ? await register(email, password)
        : await login(email, password);
      auth.tokens = data;
      onAuthed(await me());
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={submit}>
        <h1 className="brand-heading"><Logo size={30} /> Lattice</h1>
        <p className="subtitle">
          Track multiple portfolios of gold, currency, KAMA stock, and
          real estate — valued live in Tomans, with net-worth history.
        </p>

        <div className="seg">
          <button type="button" className={mode === "login" ? "active" : ""}
            onClick={() => setMode("login")}>Sign in</button>
          <button type="button" className={mode === "register" ? "active" : ""}
            onClick={() => setMode("register")}>Create account</button>
        </div>

        <label htmlFor="auth-email">Email</label>
        <input id="auth-email" type="email" required value={email}
          onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        {registering && email && !emailValid && (
          <p className="hint" style={{ color: "var(--danger, #ef4444)" }}>
            Enter a valid email address.
          </p>
        )}
        <label htmlFor="auth-password">Password</label>
        <span className="pw-field">
          <input id="auth-password" type={showPw ? "text" : "password"} required value={password}
            onChange={(e) => setPassword(e.target.value)} placeholder="min 8 chars" />
          <button type="button" className="link pw-toggle"
            onClick={() => setShowPw((v) => !v)}
            aria-label={showPw ? "Hide password" : "Show password"}>
            {showPw ? "Hide" : "Show"}
          </button>
        </span>
        {registering && password && (
          <ul className="pw-rules">
            {checks.map((c) => (
              <li key={c.label} className={c.ok ? "ok" : "muted"}>
                {c.ok ? "✓" : "•"} {c.label}
              </li>
            ))}
          </ul>
        )}

        {error && <div className="error">{error}</div>}

        <button className="primary" disabled={!canSubmit}>
          {busy ? "Please wait…" : registering ? "Create account" : "Sign in"}
        </button>
        <p className="hint">Demo: <code>demo@portfolio.local</code> / <code>demo12345</code></p>
      </form>
    </div>
  );
}

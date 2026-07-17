import { useState } from "react";
import { auth, login, me, register } from "../api.js";

export default function Auth({ onAuthed }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = mode === "login"
        ? await login(email, password)
        : await register(email, password);
      auth.tokens(data);
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
        <h1>📊 Portfolio SaaS</h1>
        <p className="subtitle">Track Iranian-market portfolios in real time.</p>

        <div className="seg">
          <button type="button" className={mode === "login" ? "active" : ""}
            onClick={() => setMode("login")}>Sign in</button>
          <button type="button" className={mode === "register" ? "active" : ""}
            onClick={() => setMode("register")}>Create account</button>
        </div>

        <label>
          Email
          <input type="email" required value={email}
            onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        </label>
        <label>
          Password
          <input type="password" required value={password}
            onChange={(e) => setPassword(e.target.value)} placeholder="min 8 chars" />
        </label>

        {error && <div className="error">{error}</div>}

        <button className="primary" disabled={busy}>
          {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
        </button>
        <p className="hint">Demo: <code>demo@portfolio.local</code> / <code>demo12345</code></p>
      </form>
    </div>
  );
}

import { useState } from "react";
import { auth, login, me, register, requestPasswordReset, sessionExpiry } from "../api.js";
import Logo from "./Logo.jsx";
import { Button, Input } from "./ui.jsx";

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
  if (
    lower.includes("unavailable") ||
    lower.includes("server") ||
    lower.includes("network") ||
    lower.includes("failed to fetch")
  ) {
    return "The service is temporarily unavailable. Please try again.";
  }
  return msg || "An unexpected error occurred.";
}

export default function Auth({ onAuthed }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [showConfirmPw, setShowConfirmPw] = useState(false);
  const [error, setError] = useState("");
  const [fieldError, setFieldError] = useState({});
  const [busy, setBusy] = useState(false);
  const [resetSent, setResetSent] = useState(false);

  const registering = mode === "signup";
  const forgetting = mode === "forgot";
  const emailValid = EMAIL_RE.test(email.trim());
  const checks = getPasswordChecks(password, confirmPassword, registering);
  const strength = calculateStrength(password, checks);
  // Memberships are closed, and `registering` is how that is said here: the
  // sign-up tab shows the notice below and its button never enables. This reads
  // like a bug -- a complete form, a working submit handler, and a button that
  // can never be pressed -- so: it is deliberate, and it mirrors
  // `REGISTRATION_OPEN = False` in the server's settings. If memberships reopen,
  // BOTH must change, plus the notice; none of the three reads the other.
  // Signing up would also need every requirement in the checklist to pass,
  // including the confirmation match, which is what `checks` is already for.
  const registrationClosed = registering;
  const canSubmit = emailValid && (forgetting || Boolean(password)) && !registrationClosed;

  const switchMode = (next) => {
    setMode(next);
    setError("");
    setFieldError({});
    setResetSent(false);
  };

  const handleEmailChange = (value) => {
    setEmail(value);
    if (value && !EMAIL_RE.test(value.trim())) {
      setFieldError((cur) => ({ ...cur, email: "Enter a valid email address." }));
    } else {
      setFieldError((cur) => {
        const next = { ...cur };
        delete next.email;
        return next;
      });
    }
  };

  const handlePasswordChange = (value) => {
    setPassword(value);
    if (!value) {
      setFieldError((cur) => ({ ...cur, password: "Password is required." }));
    } else {
      setFieldError((cur) => {
        const next = { ...cur };
        delete next.password;
        return next;
      });
    }
  };

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    setFieldError({});
    try {
      if (forgetting) {
        await requestPasswordReset(email.trim());
        setResetSent(true);
        return;
      }
      const data = registering
        ? await register(email.trim(), password)
        : await login(email.trim(), password);
      auth.tokens = data;
      sessionExpiry.set(data.session_expires_at);
      onAuthed(await me());
    } catch (err) {
      const msg = err.message || "";
      const lower = msg.toLowerCase();
      if (lower.includes("email")) {
        setFieldError({ email: msg });
      } else if (lower.includes("password") && !lower.includes("incorrect")) {
        setFieldError({ password: msg });
      }
      setError(friendlyError(msg));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <main>
      <form
        data-testid="auth-card"
        className="w-full max-w-md rounded-xl border border-border bg-panel p-5"
        onSubmit={submit}
        noValidate
      >
        <div className="mb-6">
          <h1 className="flex items-center gap-2 text-xl font-semibold text-text">
            <Logo size={36} />
            <span>Holdings</span>
          </h1>
          <p className="mt-1 text-sm text-muted">
            {forgetting
              ? "Enter the email on the account. If it exists, we send a reset link."
              : registering
                ? "Create your account to start tracking multi-asset portfolios live."
                : "Welcome back! Sign in to access your portfolios and analytics."}
          </p>
        </div>

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
              mode === "login" ? "bg-[var(--c-accent-fill)] text-white" : "text-muted hover:text-text"
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
              mode === "signup" ? "bg-[var(--c-accent-fill)] text-white" : "text-muted hover:text-text"
            }`}
          >
            Create account
          </button>
        </div>

        {error && (
          <div
            data-testid="auth-error-banner"
            role="alert"
            aria-live="polite"
            className="mb-4 rounded-lg border border-[var(--c-critical-text)] bg-panel-2 px-4 py-3 text-sm text-[var(--c-critical-text)]"
          >
            {error}
          </div>
        )}

        {registering && (
          <div
            data-testid="auth-registration-closed"
            role="status"
            className="mb-4 rounded-lg border border-[var(--c-warn-text)] bg-panel-2 px-4 py-3 text-sm text-[var(--c-warn-text)]"
          >
            New memberships are currently closed. Existing users can still sign in.
          </div>
        )}

        {resetSent && (
          <div
            data-testid="auth-reset-sent"
            role="status"
            className="mb-4 rounded-lg border border-[var(--c-good-text)] bg-panel-2 px-4 py-3 text-sm text-[var(--c-good-text)]"
          >
            If an account exists for that email, a reset link has been sent.
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
                  emailValid ? "text-[var(--c-good-text)]" : "text-[var(--c-critical-text)]"
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
            onChange={(e) => handleEmailChange(e.target.value)}
            placeholder="you@example.com"
            aria-invalid={Boolean(fieldError.email)}
            data-testid="auth-email-input"
            className="w-full"
          />
          {fieldError.email && (
            <p className="mt-1 text-xs text-[var(--c-critical-text)]">{fieldError.email}</p>
          )}
        </div>

        {!forgetting && (
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
              onChange={(e) => handlePasswordChange(e.target.value)}
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
            <p className="mt-1 text-xs text-[var(--c-critical-text)]">{fieldError.password}</p>
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

        {mode === "login" && (
          <div className="mb-4 text-right">
            <Button
              type="button"
              variant="link"
              data-testid="auth-forgot"
              onClick={() => switchMode("forgot")}
            >
              Forgot password?
            </Button>
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
                    password === confirmPassword ? "text-[var(--c-good-text)]" : "text-[var(--c-critical-text)]"
                  }`}
                >
                  {password === confirmPassword ? "✓ Match" : "Mismatch"}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Input
                id="auth-confirm-password"
                data-testid="auth-confirm-password-input"
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
                  className={`flex items-center gap-2 ${c.ok ? "text-[var(--c-good-text)]" : "text-muted"}`}
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
          disabled={busy || !canSubmit || resetSent}
          className="w-full py-1.5"
        >
          {busy ? "Working…" : forgetting ? "Send reset link" : registering ? "Create account" : "Sign in"}
        </Button>
      </form>
      </main>
    </div>
  );
}

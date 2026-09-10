import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { confirmPasswordReset } from "../api.js";
import Logo from "./Logo.jsx";
import { Button, Field, Input } from "./ui.jsx";

export default function ResetPassword() {
  const [params] = useSearchParams();
  const uid = params.get("uid") || "";
  const token = params.get("token") || "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const mismatch = confirm !== "" && password !== confirm;
  const canSubmit = uid && token && password.length >= 8 && password === confirm && !busy;

  async function submit(e) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError("");
    try {
      await confirmPasswordReset({
        uid,
        token,
        newPassword: password,
        confirmPassword: confirm,
      });
      setDone(true);
    } catch (err) {
      setError(err.message || "This reset link is invalid or has expired.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <main>
      <form
        data-testid="reset-card"
        className="w-full max-w-md rounded-xl border border-border bg-panel p-5"
        onSubmit={submit}
        noValidate
      >
        <h1 className="flex items-center gap-2 text-xl font-semibold text-text">
          <Logo size={36} />
          <span>Reset password</span>
        </h1>
        <p className="mt-1 text-sm text-muted">Choose a new password for this account.</p>

        {(!uid || !token) && (
          <p className="mt-4 text-sm text-[var(--c-critical-text)]" data-testid="reset-missing-link">
            This page needs a reset link from your email.
          </p>
        )}

        {error && (
          <p role="alert" className="mt-4 text-sm text-[var(--c-critical-text)]" data-testid="reset-error">
            {error}
          </p>
        )}

        {done ? (
          <p className="mt-4 text-sm text-[var(--c-good-text)]" data-testid="reset-done">
            Password updated.{" "}
            <Link to="/" className="underline">
              Sign in
            </Link>{" "}
            with the new password.
          </p>
        ) : (
          <>
            <div className="mt-4 space-y-3">
              <Field label="New password">
                <Input
                  label="New password"
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  data-testid="reset-password"
                  className="w-full"
                />
              </Field>
              <Field label="Confirm new password">
                <Input
                  label="Confirm new password"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  data-testid="reset-confirm"
                  className="w-full"
                />
              </Field>
              {mismatch && (
                <p className="text-xs text-[var(--c-critical-text)]">The two passwords do not match.</p>
              )}
            </div>
            <Button
              type="submit"
              variant="primary"
              data-testid="reset-submit"
              disabled={!canSubmit}
              className="mt-4 w-full py-1.5"
            >
              {busy ? "Saving…" : "Set new password"}
            </Button>
          </>
        )}

        <p className="mt-4 text-sm">
          <Link to="/" className="text-[var(--c-accent-text)] underline underline-offset-2">
            Back to sign in
          </Link>
        </p>
      </form>
      </main>
    </div>
  );
}

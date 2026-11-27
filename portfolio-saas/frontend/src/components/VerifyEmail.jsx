import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { confirmEmail } from "../api.js";
import { useT } from "../i18n.js";
import Logo from "./Logo.jsx";

// The page the confirmation email links to. It confirms on load: the link
// itself is the proof, so asking for a click as well only adds a step. The
// ref guards React's double effect run in dev -- the second POST would find
// the token already spent (it hashes the confirmation time) and show an error
// over a success.
export default function VerifyEmail() {
  const t = useT();
  const [params] = useSearchParams();
  const uid = params.get("uid") || "";
  const token = params.get("token") || "";
  const [state, setState] = useState(uid && token ? "working" : "missing");
  const sent = useRef(false);

  useEffect(() => {
    if (!uid || !token || sent.current) return;
    sent.current = true;
    confirmEmail({ uid, token })
      .then(() => setState("done"))
      .catch(() => setState("failed"));
  }, [uid, token]);

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <main>
        <div data-testid="verify-card" className="w-full max-w-md rounded-xl border border-border bg-panel p-5">
          <h1 className="flex items-center gap-2 text-xl font-semibold text-text">
            <Logo size={36} />
            <span>{t("Confirm your email")}</span>
          </h1>
          {state === "working" && (
            <p className="mt-4 text-sm text-muted" data-testid="verify-working">{t("Working…")}</p>
          )}
          {state === "missing" && (
            <p className="mt-4 text-sm text-[var(--c-critical-text)]" data-testid="verify-missing-link">
              {t("This page needs the confirmation link from your email.")}
            </p>
          )}
          {state === "failed" && (
            <p role="alert" className="mt-4 text-sm text-[var(--c-critical-text)]" data-testid="verify-error">
              {t("This confirmation link is invalid or has expired.")}{" "}
              <Link to="/login" className="underline">{t("Sign in to get a new link.")}</Link>
            </p>
          )}
          {state === "done" && (
            <p className="mt-4 text-sm text-[var(--c-good-text)]" data-testid="verify-done">
              {t("Your email is confirmed.")}{" "}
              <Link to="/login" className="underline">{t("Sign in now.")}</Link>
            </p>
          )}
        </div>
      </main>
    </div>
  );
}

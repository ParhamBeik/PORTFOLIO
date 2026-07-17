import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { createCheckout, me } from "../api.js";

// Hosts the Stripe redirect: success/cancel URLs land here as ?status=. We refresh
// the user on arrival so the PRO flip from the webhook shows up immediately.
export default function Billing({ user, setUser }) {
  const [params] = useSearchParams();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const status = params.get("status");

  useEffect(() => {
    if (status) me().then(setUser).catch(() => {});
  }, [status, setUser]);

  async function upgrade() {
    if (busy) return;
    setBusy(true);
    setErr("");
    try {
      const { url } = await createCheckout();
      window.location.href = url;
    } catch (e) {
      setErr(e.message || "Could not start checkout.");
      setBusy(false);
    }
  }

  return (
    <div className="dashboard">
      <section className="card">
        <h2>Billing</h2>
        {status === "success" && (
          <div className="ok">Thanks! Your Pro subscription is being confirmed.</div>
        )}
        {status === "cancel" && (
          <div className="muted">Checkout was cancelled — you can upgrade anytime.</div>
        )}

        <h3>Current plan</h3>
        <p className="big">{user.is_pro ? "Pro" : "Free"}</p>

        {user.is_pro ? (
          <p className="muted">
            You're on Pro. Manage or cancel your subscription in the Stripe customer portal.
          </p>
        ) : (
          <>
            <p>
              Pro unlocks allocation breakdowns, concentration-risk alerts, gold target
              bands, and your net-worth trend.
            </p>
            <button className="primary big" onClick={upgrade} disabled={busy}>
              {busy ? "Redirecting…" : "Upgrade to Pro"}
            </button>
            {err && <div className="error">{err}</div>}
          </>
        )}
      </section>
    </div>
  );
}

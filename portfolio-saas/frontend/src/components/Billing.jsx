import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { me } from "../api.js";
import { useUpgrade } from "../hooks/useUpgrade.js";

// Hosts the Zarinpal callback redirect: it lands here as ?status=success
// (optionally &ref_id=) / cancel / error. We refresh the user on arrival so the
// PRO flip from the verify step shows up immediately.
export default function Billing({ user, setUser }) {
  const [params] = useSearchParams();
  const { upgrade, busy, error: err } = useUpgrade();
  const status = params.get("status");
  const refId = params.get("ref_id");

  useEffect(() => {
    if (status === "success") me().then(setUser).catch(() => {});
  }, [status, setUser]);

  return (
    <div className="dashboard">
      <section className="card">
        <h2>Billing</h2>
        {status === "success" && (
          <div className="ok">
            Thanks — your Pro subscription is active!
            {refId && <span className="muted small"> (ref {refId})</span>}
          </div>
        )}
        {status === "cancel" && (
          <div className="muted">Payment was cancelled — you can upgrade anytime.</div>
        )}
        {status === "error" && (
          <div className="error">We could not verify the payment. If you were charged, contact support.</div>
        )}

        <h3>Current plan</h3>
        <p className="big">{user.is_pro ? "Pro" : "Free"}</p>

        {user.is_pro ? (
          <p className="muted">
            You're on Pro (annual). It renews here each year — no auto-rebilling in between.
          </p>
        ) : (
          <>
            <p>
              Pro unlocks risk analytics (Sharpe, drawdown, VaR), portfolio optimization
              (max Sharpe, risk parity, HRP), and your efficient frontier.
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

import { useEffect, useState } from "react";
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
  const [refreshing, setRefreshing] = useState(status === "success");
  const [refreshError, setRefreshError] = useState("");
  const [activationConfirmed, setActivationConfirmed] = useState(false);

  useEffect(() => {
    if (status !== "success") return;
    let current = true;
    setRefreshing(true);
    setRefreshError("");
    me()
      .then((nextUser) => {
        if (current) {
          setUser(nextUser);
          setActivationConfirmed(Boolean(nextUser.is_pro));
        }
      })
      .catch((error) => {
        if (current) setRefreshError(error.message);
      })
      .finally(() => {
        if (current) setRefreshing(false);
      });
    return () => { current = false; };
  }, [status, setUser]);

  return (
    <div className="dashboard">
      <section className="card">
        <h2>Billing</h2>
        {status === "success" && (
          <div className={refreshing || activationConfirmed ? "ok" : "error"} role="status">
            {refreshing
              ? "Payment returned successfully — confirming your plan…"
              : activationConfirmed
              ? "Thanks — your Pro subscription is active!"
              : "Payment returned successfully, but Pro is not active yet. Retry shortly or contact support if you were charged."}
            {refId && <span className="muted small"> (ref {refId})</span>}
          </div>
        )}
        {refreshError && (
          <div className="error">
            Payment succeeded, but the plan could not refresh: {refreshError}
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
            You're on Pro (annual)
            {user.pro_expires_at && (
              <>
                {" "}until{" "}
                <time dateTime={user.pro_expires_at}>
                  {new Intl.DateTimeFormat("en-US", {
                    dateStyle: "medium",
                    timeZone: "Asia/Tehran",
                  }).format(new Date(user.pro_expires_at))}
                </time>
              </>
            )}
            . Renew here each year; there is no automatic rebilling.
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

import { useEffect, useState } from "react";
import { insights as fetchInsights, createCheckout } from "../api.js";

export default function Insights({ user, setUser }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [checkoutBusy, setCheckoutBusy] = useState(false);
  const [checkoutErr, setCheckoutErr] = useState("");

  async function upgrade() {
    if (checkoutBusy) return;
    setCheckoutBusy(true);
    setCheckoutErr("");
    try {
      const { url } = await createCheckout();
      window.location.href = url; // hand off to Stripe hosted Checkout
    } catch (e) {
      setCheckoutErr(e.message || "Could not start checkout.");
      setCheckoutBusy(false);
    }
  }

  useEffect(() => {
    if (!user.is_pro) return;
    fetchInsights()
      .then((d) => { setData(d); setError(""); })
      .catch((e) => setError(e.message));
  }, [user.is_pro]);

  if (!user.is_pro) {
    return (
      <div className="upsell">
        <h2>🔒 Advanced insights are a Pro feature</h2>
        <p>
          Upgrade to see allocation breakdowns, concentration-risk alerts,
          target-band suggestions, and your net-worth trend.
        </p>
        <button className="primary big" onClick={upgrade} disabled={checkoutBusy}>
          {checkoutBusy ? "Redirecting…" : "Upgrade to Pro"}
        </button>
        {checkoutErr && <p className="error small">{checkoutErr}</p>}
      </div>
    );
  }

  return (
    <div className="insights">
      <div className="insights-head">
        <h2>Advanced Insights</h2>
      </div>
      {error && <div className="error">{error}</div>}
      {!data && !error && <p className="muted">Crunching numbers…</p>}

      {data && (
        <>
          <section className="card">
            <h3>Allocation by asset class</h3>
            <div className="alloc">
              {Object.entries(data.allocation).map(([cls, pct]) => (
                <div key={cls} className="bar-row">
                  <span className="bar-label">{cls}</span>
                  <div className="bar">
                    <div className="bar-fill" style={{ width: `${pct}%` }} />
                  </div>
                  <span className="bar-val">{pct}%</span>
                </div>
              ))}
            </div>
          </section>

          <section className="card">
            <h3>Concentration risk</h3>
            <Insight i={data.concentration} />
            <h3>Gold target band</h3>
            <Insight i={data.gold_band} />
            <h3>Net-worth trend</h3>
            <Insight i={data.net_worth_trend} />
          </section>
        </>
      )}
    </div>
  );
}

function Insight({ i }) {
  if (!i) return null;
  const tone = i.severity || "info";
  return <div className={`insight ${tone}`}>{i.message}</div>;
}

import { useEffect, useState } from "react";
import { insights as fetchInsights } from "../api.js";
import ProGate from "./ProGate.jsx";

export default function Insights({ user, account = null }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!user.is_pro) return;
    let current = true;
    fetchInsights(account)
      .then((d) => { if (current) { setData(d); setError(""); } })
      .catch((e) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [user.is_pro, account]);

  return (
    <ProGate
      user={user}
      pitch="Upgrade to see allocation breakdowns, concentration-risk alerts, target-band suggestions, and your net-worth trend."
    >
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
    </ProGate>
  );
}

function Insight({ i }) {
  if (!i) return null;
  const tone = i.severity || "info";
  return <div className={`insight ${tone}`}>{i.message}</div>;
}

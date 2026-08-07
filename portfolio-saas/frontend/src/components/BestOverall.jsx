import { useEffect, useMemo, useState } from "react";
import { bestOverall, listAssets } from "../api.js";
import { fmtPct } from "../format.js";
import ProGate from "./ProGate.jsx";

const pctOrDash = (v) => (v == null ? "—" : fmtPct(v * 100));
const numOrDash = (v) => (v == null ? "—" : v.toFixed(2));

// "Not limited to my current assets": the best portfolio across every tracked
// asset, over 1/3/5/10-year windows. Pure read of a nightly precompute (see
// portfolio/services/best_overall.py) -- no solver call happens on this page load.
export default function BestOverall({ user }) {
  const [data, setData] = useState(null);
  const [windowLabel, setWindowLabel] = useState("1Y");
  const [scenario, setScenario] = useState("max_sharpe");
  const [catalog, setCatalog] = useState([]);
  const [err, setErr] = useState("");
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    listAssets().then(setCatalog).catch(() => {});
  }, []);

  useEffect(() => {
    if (!user.is_pro) return;
    let current = true;
    setErr("");
    setData(null);
    bestOverall()
      .then((r) => { if (current) setData(r); })
      .catch((e) => { if (current) setErr(e.message); });
    return () => { current = false; };
  }, [user.is_pro, retryKey]);

  const labelOf = useMemo(() => {
    const m = new Map(catalog.map((a) => [a.key, a.name_fa || a.name]));
    return (k) => m.get(k) || k;
  }, [catalog]);

  const win = data?.windows?.find((w) => w.label === windowLabel) || null;
  const result = win?.[scenario] || null;
  const weightRows = result
    ? Object.entries(result.target_weights).sort((a, b) => b[1] - a[1])
    : [];

  return (
    <ProGate
      user={user}
      pitch="Upgrade to see the best possible portfolio across every tracked asset -- stocks, currencies, gold, and more -- over 1/3/5/10-year windows."
    >
      <div className="insights">
        <div className="insights-head">
          <h2>Best Possible Portfolio Overall</h2>
          <p className="muted small">
            The ideal portfolio across ALL tracked assets (not just what you hold), computed
            nightly. {data?.as_of && <>As of {new Date(data.as_of).toLocaleString()}.</>}
          </p>
        </div>

        {err && (
          <div className="error inline" role="alert">
            <span>Could not load: {err}</span>
            <button type="button" className="link" onClick={() => setRetryKey((k) => k + 1)}>
              Retry
            </button>
          </div>
        )}
        {!data && !err && <p className="muted" role="status">Loading…</p>}

        {data && (
          <>
            <section className="card">
              <div className="scenario-row" role="group" aria-label="Lookback window">
                {data.windows.map((w) => (
                  <button
                    key={w.label}
                    className={windowLabel === w.label ? "primary" : ""}
                    aria-pressed={windowLabel === w.label}
                    onClick={() => setWindowLabel(w.label)}
                  >
                    {w.label}
                  </button>
                ))}
              </div>
              <div className="scenario-row" role="group" aria-label="Scenario" style={{ marginTop: "0.5rem" }}>
                {[
                  { key: "max_sharpe", label: "Max Sharpe" },
                  { key: "min_volatility", label: "Min Volatility" },
                ].map((s) => (
                  <button
                    key={s.key}
                    className={scenario === s.key ? "primary" : ""}
                    aria-pressed={scenario === s.key}
                    disabled={!win?.[s.key]}
                    onClick={() => setScenario(s.key)}
                  >
                    {s.label}
                  </button>
                ))}
              </div>

              {win?.status === "insufficient_history" && (
                <p className="muted" role="status">
                  Not enough warehouse history yet for the {win.label} window.
                </p>
              )}

              {result && (
                <>
                  <p className="muted small" style={{ marginTop: "0.75rem" }}>
                    Annualized return {pctOrDash(result.target_metrics.expected_return_annual)} ·
                    volatility {pctOrDash(result.target_metrics.annualized_volatility)} ·
                    Sharpe {numOrDash(result.target_metrics.sharpe)}
                  </p>
                  <table className="holdings" style={{ marginTop: "0.75rem" }}>
                    <thead>
                      <tr>
                        <th>Asset</th>
                        <th>Weight</th>
                      </tr>
                    </thead>
                    <tbody>
                      {weightRows.map(([key, weight]) => (
                        <tr key={key}>
                          <td>{labelOf(key)}</td>
                          <td>{fmtPct(weight * 100)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
            </section>

            {Object.keys(data.leaders || {}).length > 0 && (
              <section className="card">
                <h3>Top performers by asset class</h3>
                <p className="muted small">
                  Trailing 1-year risk-adjusted performance
                  {data.leaders_as_of && <> as of {data.leaders_as_of}</>}.
                </p>
                {Object.entries(data.leaders).map(([category, rows]) => (
                  <div key={category} style={{ marginTop: "1rem" }}>
                    <h4 className="subhead">{category}</h4>
                    <table className="holdings">
                      <thead>
                        <tr>
                          <th>Symbol</th>
                          <th>Sharpe</th>
                          <th>Sortino</th>
                          <th>Return</th>
                          <th>Volatility</th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.slice(0, 5).map((row) => (
                          <tr key={row.symbol}>
                            <td>{row.name}</td>
                            <td>{numOrDash(row.sharpe)}</td>
                            <td>{numOrDash(row.sortino)}</td>
                            <td>{pctOrDash(row.expected_return_annual)}</td>
                            <td>{pctOrDash(row.volatility_annual)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ))}
              </section>
            )}
          </>
        )}
      </div>
    </ProGate>
  );
}

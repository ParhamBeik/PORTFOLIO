import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ReferenceDot,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { frontier, listAssets, myOptimal } from "../api.js";
import { fmtPct, fmtToman } from "../format.js";
import ProGate from "./ProGate.jsx";
import { usePortfolio } from "./PortfolioContext.jsx";

const signedPct = (value) => `${value >= 0 ? "+" : ""}${fmtPct(value)}`;
const signedToman = (value) => `${Number(value) >= 0 ? "+" : "-"}${fmtToman(Math.abs(Number(value)))}`;
const pctOrDash = (v) => (v == null ? "—" : fmtPct(v * 100));
const numOrDash = (v) => (v == null ? "—" : v.toFixed(2));

// "If a quant analyst had optimized MY existing assets using historical data,
// what would it look like?" -- one API call returns all four lookback windows
// (1Y/3Y/5Y/Lifetime), each with max-Sharpe + min-volatility targets over the
// user's OWN held assets, next to how the portfolio actually performed.
export default function Optimization({ user }) {
  const { activeId: account } = usePortfolio();
  const [windows, setWindows] = useState(null);
  const [windowLabel, setWindowLabel] = useState("1Y");
  const [scenario, setScenario] = useState("max_sharpe");
  const [front, setFront] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [err, setErr] = useState("");
  const [frontErr, setFrontErr] = useState("");
  const [retryKey, setRetryKey] = useState(0);
  const [frontRetryKey, setFrontRetryKey] = useState(0);

  useEffect(() => {
    listAssets().then(setCatalog).catch(() => {});
  }, []);

  useEffect(() => {
    if (!user.is_pro) return;
    let current = true;
    setErr("");
    setWindows(null);
    myOptimal(account)
      .then((r) => { if (current) { setWindows(r.windows); setErr(""); } })
      .catch((e) => { if (current) { setWindows(null); setErr(e.message); } });
    return () => { current = false; };
  }, [user.is_pro, account, retryKey]);

  useEffect(() => {
    if (!user.is_pro) return;
    let current = true;
    setFront(null);
    setFrontErr("");
    frontier(account)
      .then((value) => { if (current) setFront(value); })
      .catch((error) => { if (current) setFrontErr(error.message); });
    return () => { current = false; };
  }, [user.is_pro, account, frontRetryKey]);

  const labelOf = useMemo(() => {
    const m = new Map(catalog.map((a) => [a.key, a.name_fa || a.name]));
    return (k) => m.get(k) || k;
  }, [catalog]);

  const win = windows?.find((w) => w.label === windowLabel) || null;
  const result = win?.status === "ok" ? win[scenario] : null;

  const keys = result
    ? Array.from(new Set([...Object.keys(result.current_weights), ...Object.keys(result.target_weights)]))
    : [];
  const barData = keys.map((k) => ({
    name: labelOf(k),
    current: (result.current_weights[k] || 0) * 100,
    target: (result.target_weights[k] || 0) * 100,
  }));
  const frontierPts = (front?.frontier || []).map((p) => ({
    x: p.volatility * 100,
    y: p.return * 100,
  }));
  const cloudPts = (front?.cloud || []).map((p) => ({
    x: p.volatility * 100,
    y: p.return * 100,
  }));
  const curPt = front?.current
    ? { x: front.current.volatility * 100, y: front.current.return * 100 }
    : null;
  const msPt = front?.max_sharpe?.metrics
    ? {
        x: front.max_sharpe.metrics.annualized_volatility * 100,
        y: front.max_sharpe.metrics.expected_return_annual * 100,
      }
    : null;

  // Actual vs Max-Sharpe vs Min-Variance, side by side, for the selected window.
  const comparisonRows = win?.status === "ok" ? (() => {
    const actualReturn = win.actual.metrics.sharpe * win.actual.metrics.annualized_volatility + win.actual.risk_free_rate_annual;
    const cols = {
      actual: { label: "Actual", return: actualReturn, vol: win.actual.metrics.annualized_volatility, sharpe: win.actual.metrics.sharpe, mdd: win.actual.metrics.max_drawdown },
      max_sharpe: win.max_sharpe ? {
        label: "Max Sharpe", return: win.max_sharpe.target_metrics.expected_return_annual,
        vol: win.max_sharpe.target_metrics.annualized_volatility, sharpe: win.max_sharpe.target_metrics.sharpe,
        mdd: win.max_sharpe.diagnostics?.metrics?.max_drawdown,
      } : null,
      min_volatility: win.min_volatility ? {
        label: "Min Variance", return: win.min_volatility.target_metrics.expected_return_annual,
        vol: win.min_volatility.target_metrics.annualized_volatility, sharpe: win.min_volatility.target_metrics.sharpe,
        mdd: win.min_volatility.diagnostics?.metrics?.max_drawdown,
      } : null,
    };
    return cols;
  })() : null;

  return (
    <ProGate
      user={user}
      pitch="Upgrade to see what a quant-optimized version of your own portfolio would look like across 1/3/5-year and lifetime lookback windows, compared with your actual performance."
    >
    <div className="insights">
      <div className="insights-head">
        <h2>Optimized Version of My Portfolio</h2>
        <p className="muted small">
          Max-Sharpe and min-volatility allocations of your OWN held assets, over several
          historical lookback windows, next to how you actually performed.
        </p>
      </div>

      {err && (
        <div className="error inline" role="alert">
          <span>Could not load: {err}</span>
          <button type="button" className="link" onClick={() => setRetryKey((key) => key + 1)}>
            Retry
          </button>
        </div>
      )}
      {!windows && !err && <p className="muted" role="status">Calculating…</p>}

      {windows && (
        <section className="card">
          <div className="scenario-row" role="group" aria-label="Lookback window">
            {windows.map((w) => (
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
          <p className="muted small">{win?.window_days} day window.</p>

          {win?.status === "insufficient_history" && (
            <p className="muted" role="status">
              Not enough price history yet for the {win.label} window: {win.detail}
            </p>
          )}

          {comparisonRows && (
            <table className="holdings" style={{ marginTop: "1rem" }}>
              <thead>
                <tr>
                  <th>Metric</th>
                  {Object.values(comparisonRows).filter(Boolean).map((c) => (
                    <th key={c.label}>{c.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Annualized return</td>
                  {Object.values(comparisonRows).filter(Boolean).map((c) => (
                    <td key={c.label}>{pctOrDash(c.return)}</td>
                  ))}
                </tr>
                <tr>
                  <td>Annualized volatility</td>
                  {Object.values(comparisonRows).filter(Boolean).map((c) => (
                    <td key={c.label}>{pctOrDash(c.vol)}</td>
                  ))}
                </tr>
                <tr>
                  <td>Sharpe</td>
                  {Object.values(comparisonRows).filter(Boolean).map((c) => (
                    <td key={c.label}>{numOrDash(c.sharpe)}</td>
                  ))}
                </tr>
                <tr>
                  <td>Max drawdown</td>
                  {Object.values(comparisonRows).filter(Boolean).map((c) => (
                    <td key={c.label}>{pctOrDash(c.mdd)}</td>
                  ))}
                </tr>
              </tbody>
            </table>
          )}
        </section>
      )}

      {win?.status === "ok" && (
        <>
          <section className="card">
            <h3>Scenario</h3>
            <div className="scenario-row" role="group" aria-label="Optimization scenario">
              {[
                { key: "max_sharpe", label: "Max Sharpe" },
                { key: "min_volatility", label: "Min Volatility" },
              ].map((s) => (
                <button
                  key={s.key}
                  className={scenario === s.key ? "primary" : ""}
                  aria-pressed={scenario === s.key}
                  disabled={!win[s.key]}
                  onClick={() => setScenario(s.key)}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </section>

          {result && (
            <>
              <section className="card">
                <h3>Current vs target allocation</h3>
                {result.cached && (
                  <p className="muted small">Cached result (recomputed when prices refresh).</p>
                )}
                <div
                  className="chart-wrap"
                  style={{ height: 280 }}
                  role="img"
                  aria-label="Current and target portfolio allocation comparison"
                >
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={barData} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
                      <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                      <XAxis
                        dataKey="name"
                        tick={{ fontSize: 11, fill: "var(--muted)" }}
                        interval={0}
                        angle={-20}
                        textAnchor="end"
                        height={60}
                        stroke="var(--border)"
                      />
                      <YAxis
                        tickFormatter={(v) => v + "%"}
                        tick={{ fontSize: 11, fill: "var(--muted)" }}
                        width={44}
                        stroke="var(--border)"
                      />
                      <Tooltip
                        formatter={(v) => fmtPct(v)}
                        contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 8 }}
                      />
                      <Legend />
                      <Bar dataKey="current" fill="#8b97a3" name="Current" />
                      <Bar dataKey="target" fill="#4c9aff" name="Target" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
                <details className="assumptions-panel">
                  <summary>Data window, assumptions, and exclusions</summary>
                  <dl className="assumptions-grid">
                    <div><dt>Window</dt><dd>{result.data_window ? `${result.data_window.start} to ${result.data_window.end}` : "Not reported"}</dd></div>
                    <div><dt>Observations</dt><dd>{result.observations ?? "Not reported"}</dd></div>
                    <div><dt>Risk-free rate</dt><dd>{result.risk_free_rate_annual != null ? fmtPct(result.risk_free_rate_annual * 100) : "Not reported"}</dd></div>
                  </dl>
                  {result.excluded_assets?.length > 0 && (
                    <ul className="compact-list">
                      {result.excluded_assets.map((item) => (
                        <li key={item.key || item.symbol}>
                          {labelOf(item.key || item.symbol)} — {(item.reason || "excluded").replaceAll("_", " ")}
                        </li>
                      ))}
                    </ul>
                  )}
                  {result.limitations?.map((limitation) => <p key={limitation} className="muted small">{limitation}</p>)}
                </details>
              </section>

              <section className="card">
                <h3>Rebalance trades (hypothetical)</h3>
                {result.rebalance_trades.length === 0 ? (
                  <p className="muted">The current allocation already matches this scenario.</p>
                ) : (
                  <table className="holdings">
                    <thead>
                      <tr>
                        <th>Asset</th>
                        <th>Action</th>
                        <th>Current</th>
                        <th>Target</th>
                        <th>Δ weight</th>
                        <th>Δ value (T)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.rebalance_trades.map((t) => {
                        const current = (result.current_weights[t.key] || 0) * 100;
                        const target = (result.target_weights[t.key] || 0) * 100;
                        const delta = target - current;
                        const signedValue = Math.abs(Number(t.delta_value_tomans)) * (t.action === "buy" ? 1 : -1);
                        return (
                          <tr key={t.key}>
                            <td>{labelOf(t.key)}</td>
                            <td className={t.action === "buy" ? "pos" : "neg"}>
                              {t.action.toUpperCase()}
                            </td>
                            <td>{fmtPct(current)}</td>
                            <td>{fmtPct(target)}</td>
                            <td className={delta >= 0 ? "pos" : "neg"}>{signedPct(delta)} pp</td>
                            <td className={signedValue >= 0 ? "pos" : "neg"}>
                              {signedToman(signedValue)}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </section>
            </>
          )}
        </>
      )}

      {front && frontierPts.length > 1 && (
        <section className="card">
          <h3>Efficient frontier</h3>
          <div
            className="chart-wrap"
            style={{ height: 320 }}
            role="img"
            aria-label="Efficient frontier showing historical estimated return versus volatility"
          >
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 8, right: 16, left: 8, bottom: 24 }}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                <XAxis
                  type="number"
                  dataKey="x"
                  name="Volatility"
                  unit="%"
                  tick={{ fontSize: 11, fill: "var(--muted)" }}
                  label={{ value: "Volatility (%)", position: "insideBottom", offset: -12, fontSize: 11, fill: "var(--muted)" }}
                  stroke="var(--border)"
                />
                <YAxis
                  type="number"
                  dataKey="y"
                  name="Return"
                  unit="%"
                  tick={{ fontSize: 11, fill: "var(--muted)" }}
                  width={48}
                  label={{ value: "Return (%)", angle: -90, position: "insideLeft", fontSize: 11, fill: "var(--muted)" }}
                  stroke="var(--border)"
                />
                <ZAxis range={[40, 40]} />
                <Tooltip
                  cursor={{ strokeDasharray: "3 3" }}
                  formatter={(v) => (typeof v === "number" ? fmtPct(v) : v)}
                  contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 8 }}
                />
                {cloudPts.length > 0 && (
                  <Scatter data={cloudPts} fill="#8b97a3" fillOpacity={0.25} shape="circle" legendType="none" />
                )}
                <Scatter data={frontierPts} fill="#4c9aff" />
                {msPt && (
                  <ReferenceDot x={msPt.x} y={msPt.y} r={6} fill="#3fb950" stroke="#fff" />
                )}
                {curPt && (
                  <ReferenceDot x={curPt.x} y={curPt.y} r={6} fill="#f85149" stroke="#fff" />
                )}
              </ScatterChart>
            </ResponsiveContainer>
          </div>
          <p className="muted small">
            Each blue dot is a hypothetical minimum-volatility allocation at a historical target return.{" "}
            The faint gray cloud is 400 random reweightings of your own held assets, showing the range
            your current mix could reach without adding anything new.{" "}
            <span style={{ color: "#f85149" }}>●</span> your current portfolio ·{" "}
            <span style={{ color: "#3fb950" }}>●</span> max-Sharpe portfolio.
          </p>
        </section>
      )}
      {frontErr && (
        <div className="error inline small" role="alert">
          <span>Efficient frontier unavailable: {frontErr}</span>
          <button type="button" className="link" onClick={() => setFrontRetryKey((key) => key + 1)}>
            Retry
          </button>
        </div>
      )}
    </div>
    </ProGate>
  );
}

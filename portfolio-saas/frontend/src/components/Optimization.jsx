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
import { frontier, listAssets, optimize } from "../api.js";
import { fmtPct, fmtToman } from "../format.js";
import ProGate from "./ProGate.jsx";
import { usePortfolio } from "./PortfolioContext.jsx";

const SCENARIOS = [
  { key: "max_sharpe", label: "Max Sharpe", hint: "Best risk-adjusted return" },
  { key: "min_volatility", label: "Min Volatility", hint: "Lowest risk" },
  { key: "risk_parity", label: "Risk Parity", hint: "Equal risk contribution (ERC)" },
  { key: "hrp", label: "HRP", hint: "Hierarchical risk parity" },
];

const signedPct = (value) => `${value >= 0 ? "+" : ""}${fmtPct(value)}`;
const signedToman = (value) => `${Number(value) >= 0 ? "+" : "-"}${fmtToman(Math.abs(Number(value)))}`;

// Pro: scenario optimizer + efficient frontier. The frontier is fetched once;
// re-running a scenario hits the cached returns matrix on the backend.
export default function Optimization({ user }) {
  const { activeId: account } = usePortfolio();
  const [scenario, setScenario] = useState("max_sharpe");
  const [result, setResult] = useState(null);
  const [front, setFront] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [err, setErr] = useState("");
  const [frontErr, setFrontErr] = useState("");

  useEffect(() => {
    listAssets().then(setCatalog).catch(() => {});
  }, []);

  useEffect(() => {
    if (!user.is_pro) return;
    let current = true;
    setErr("");
    setResult(null);
    optimize(scenario, null, account)
      .then((r) => { if (current) { setResult(r); setErr(""); } })
      .catch((e) => { if (current) { setResult(null); setErr(e.message); } });
    return () => { current = false; };
  }, [scenario, user.is_pro, account]);

  useEffect(() => {
    if (!user.is_pro) return;
    let current = true;
    setFront(null);
    setFrontErr("");
    frontier(account)
      .then((value) => { if (current) setFront(value); })
      .catch((error) => { if (current) setFrontErr(error.message); });
    return () => { current = false; };
  }, [user.is_pro, account]);

  const labelOf = useMemo(() => {
    const m = new Map(catalog.map((a) => [a.key, a.name_fa || a.name]));
    return (k) => m.get(k) || k;
  }, [catalog]);

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
  const curPt = front?.current
    ? { x: front.current.volatility * 100, y: front.current.return * 100 }
    : null;
  const msPt = front?.max_sharpe?.metrics
    ? {
        x: front.max_sharpe.metrics.annualized_volatility * 100,
        y: front.max_sharpe.metrics.expected_return_annual * 100,
      }
    : null;

  return (
    <ProGate
      user={user}
      pitch="Upgrade for mean-variance (max Sharpe / min volatility), risk-parity, and hierarchical risk-parity allocation scenarios, plus your efficient frontier plotted against your current portfolio."
    >
    <div className="insights">
      <div className="insights-head">
        <h2>Portfolio Optimization</h2>
      </div>
      {err && <div className="error">{err}</div>}
      {!result && !err && <p className="muted" role="status">Calculating allocation…</p>}

      <section className="card">
        <h3>Scenario</h3>
        <div className="scenario-row">
          {SCENARIOS.map((s) => (
            <button
              key={s.key}
              className={scenario === s.key ? "primary" : ""}
              title={s.hint}
              aria-pressed={scenario === s.key}
              disabled={!result && !err}
              onClick={() => setScenario(s.key)}
            >
              {s.label}
            </button>
          ))}
        </div>
        <p className="muted small">{SCENARIOS.find((s) => s.key === scenario)?.hint}</p>
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
            {result.target_metrics && (
              <p className="muted small">
                Projected: return {fmtPct(result.target_metrics.expected_return_annual * 100)} ·
                volatility {fmtPct(result.target_metrics.annualized_volatility * 100)} ·
                Sharpe {result.target_metrics.sharpe.toFixed(2)}
              </p>
            )}
          </section>

          <section className="card">
            <h3>Rebalance trades</h3>
            {result.rebalance_trades.length === 0 ? (
              <p className="muted">Already at the target — no trades needed.</p>
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

      {front && frontierPts.length > 1 && (
        <section className="card">
          <h3>Efficient frontier</h3>
          <div
            className="chart-wrap"
            style={{ height: 320 }}
            role="img"
            aria-label="Efficient frontier showing expected return versus volatility"
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
            Each dot is a min-volatility portfolio at a target return.{" "}
            <span style={{ color: "#f85149" }}>●</span> your current portfolio ·{" "}
            <span style={{ color: "#3fb950" }}>●</span> max-Sharpe portfolio.
          </p>
        </section>
      )}
      {frontErr && <p className="error small">Efficient frontier unavailable: {frontErr}</p>}
    </div>
    </ProGate>
  );
}

import { useEffect, useMemo, useState } from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { analytics, assetReturns, listAssets } from "../api.js";
import { fmtPct, fmtToman } from "../format.js";
import ProGate from "./ProGate.jsx";

const METRIC_DEFS = [
  { key: "annualized_volatility", label: "Volatility (ann.)", fmt: (v) => fmtPct(v * 100) },
  { key: "sharpe", label: "Sharpe ratio", fmt: (v) => v.toFixed(2) },
  { key: "sortino", label: "Sortino ratio", fmt: (v) => v.toFixed(2) },
  { key: "max_drawdown", label: "Max drawdown", fmt: (v) => fmtPct(v * 100) },
  { key: "calmar", label: "Calmar ratio", fmt: (v) => v.toFixed(2) },
  { key: "historical_var_95", label: "VaR 95% (1d)", fmt: (v) => fmtPct(v * 100) },
  { key: "historical_cvar_95", label: "CVaR 95% (1d)", fmt: (v) => fmtPct(v * 100) },
  { key: "diversification_ratio", label: "Diversification", fmt: (v) => v.toFixed(2) + "×" },
];

const PALETTE = ["#4c9aff", "#3fb950", "#d29922", "#f85149", "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16", "#14b8a6", "#f97316"];

// Pro: risk/return diagnostics + allocation + correlation. Mirrors the Insights
// upsell for non-Pro visitors so deep links don't dead-end.
export default function Analytics({ user }) {
  const [data, setData] = useState(null);
  const [corr, setCorr] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!user.is_pro) return;
    listAssets().then(setCatalog).catch(() => {});
    analytics().then((d) => { setData(d); setErr(""); }).catch((e) => setErr(e.message));
    assetReturns(180).then((r) => setCorr(r.correlation)).catch(() => {});
  }, [user.is_pro]);

  const labelOf = useMemo(() => {
    const m = new Map(catalog.map((a) => [a.key, a.name_fa || a.name]));
    return (k) => m.get(k) || k;
  }, [catalog]);

  const weights = data
    ? Object.entries(data.current_weights).sort((a, b) => b[1] - a[1])
    : [];
  const pieData = weights.map(([k, v]) => ({ name: labelOf(k), value: v }));
  const re = data?.real_estate;

  return (
    <ProGate
      user={user}
      pitch="Upgrade for risk-adjusted metrics (Sharpe, Sortino, Calmar), max drawdown, daily Value-at-Risk, and a correlation map of your holdings."
    >
    <div className="insights">
      <div className="insights-head">
        <h2>Portfolio Analytics</h2>
      </div>
      {err && <div className="error">{err}</div>}
      {!data && !err && <p className="muted">Crunching numbers…</p>}
      {data && (
        <>
          <section className="card">
            <h3>Risk &amp; return</h3>
            <div className="metric-grid">
              {METRIC_DEFS.map((m) => (
                <div key={m.key} className="metric">
                  <div className="metric-val">{m.fmt(data.metrics[m.key])}</div>
                  <div className="metric-label">{m.label}</div>
                </div>
              ))}
            </div>
            {data.excluded_assets?.length > 0 && (
              <p className="muted small">
                Excluded (insufficient history):{" "}
                {data.excluded_assets.map((e) => labelOf(e.key)).join(", ")}
              </p>
            )}
          </section>

          <section className="card">
            <h3>Allocation</h3>
            <div className="alloc-grid">
              <div className="chart-wrap" style={{ height: 220, minWidth: 220 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={pieData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="50%"
                      innerRadius={50}
                      outerRadius={80}
                      paddingAngle={2}
                    >
                      {pieData.map((_, i) => (
                        <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                      ))}
                    </Pie>
                    <Tooltip
                      formatter={(v) => fmtPct(v * 100)}
                      contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 8 }}
                    />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              <div className="legend">
                {weights.map(([k, v], i) => (
                  <div key={k} className="legend-row">
                    <span className="dot" style={{ background: PALETTE[i % PALETTE.length] }} />
                    <span>{labelOf(k)}</span>
                    <span className="muted">{fmtPct(v * 100)}</span>
                  </div>
                ))}
              </div>
            </div>
            {re && Number(re.value_tomans) > 0 && (
              <p className="muted small">
                Real estate: {fmtToman(re.value_tomans)} ({fmtPct(re.share_of_total * 100)} of
                total — held aside; the metrics above are liquid-only).
              </p>
            )}
          </section>

          {corr && corr.assets.length > 1 && (
            <section className="card">
              <h3>Correlation</h3>
              <p className="muted small">
                Daily-return correlation. Red = moves together, blue = moves opposite.
              </p>
              <CorrHeatmap assets={corr.assets.map(labelOf)} matrix={corr.matrix} />
            </section>
          )}
        </>
      )}
    </div>
    </ProGate>
  );
}

function CorrHeatmap({ assets, matrix }) {
  return (
    <div className="heatmap">
      <div className="hm-row hm-head">
        <span />
        {assets.map((a) => (
          <span key={a} className="hm-col" title={a}>{a.slice(0, 6)}</span>
        ))}
      </div>
      {assets.map((a, i) => (
        <div key={a} className="hm-row">
          <span className="hm-row-label" title={a}>{a.slice(0, 8)}</span>
          {matrix[i].map((v, j) => {
            const val = Number.isFinite(v) ? v : 0;
            const bg =
              val >= 0
                ? `rgba(248,81,73,${Math.min(Math.abs(val), 1) * 0.75})`
                : `rgba(76,154,255,${Math.min(Math.abs(val), 1) * 0.75})`;
            return (
              <span key={j} className="hm-cell" style={{ background: bg }} title={`${a} / ${assets[j]}: ${val.toFixed(2)}`}>
                {val.toFixed(2)}
              </span>
            );
          })}
        </div>
      ))}
    </div>
  );
}

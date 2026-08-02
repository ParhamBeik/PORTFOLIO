import { useEffect, useMemo, useState } from "react";
import { Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { analytics, assetReturns, listAssets } from "../api.js";
import { fmtPct, fmtToman } from "../format.js";
import ProGate from "./ProGate.jsx";
import { usePortfolio } from "./PortfolioContext.jsx";

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
  const { activeId: account } = usePortfolio();
  const [data, setData] = useState(null);
  const [corr, setCorr] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [err, setErr] = useState("");
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    if (!user.is_pro) return;
    let current = true;
    setData(null);
    setCorr(null);
    setErr("");
    Promise.all([listAssets(), analytics(account), assetReturns(180)])
      .then(([assets, diagnostics, returns]) => {
        if (!current) return;
        setCatalog(assets);
        setData(diagnostics);
        setCorr(returns.correlation);
        setErr("");
      })
      .catch((e) => { if (current) setErr(e.message); });
    return () => { current = false; };
  }, [user.is_pro, account, retryKey]);

  const labelOf = useMemo(() => {
    const m = new Map(catalog.map((a) => [a.key, a.name_fa || a.name]));
    return (k) => m.get(k) || k;
  }, [catalog]);

  const weights = data
    ? Object.entries(data.current_weights).sort((a, b) => b[1] - a[1])
    : [];
  const pieData = weights.map(([k, v]) => ({ name: labelOf(k), value: v }));
  const scopedCorr = useMemo(() => {
    if (!corr) return null;
    const wanted = new Set(weights.map(([key]) => key));
    const indexes = corr.assets
      .map((key, index) => (wanted.has(key) ? index : -1))
      .filter((index) => index >= 0);
    return {
      assets: indexes.map((index) => corr.assets[index]),
      matrix: indexes.map((row) => indexes.map((column) => corr.matrix[row][column])),
    };
  }, [corr, data]);
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
      {err && (
        <div className="error inline" role="alert">
          <span>Diagnostics unavailable: {err}</span>
          <button type="button" className="link" onClick={() => setRetryKey((key) => key + 1)}>
            Retry
          </button>
        </div>
      )}
      {!data && !err && <p className="muted" role="status">Calculating historical diagnostics…</p>}
      {data && (
        <>
          <section className="card">
            <h3>Risk &amp; return</h3>
            <p className="muted small">
              Hypothetical fixed-weight exposure using the current allocation; this is not actual account performance.
            </p>
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
                Excluded: {data.excluded_assets.map((item) => (
                  `${labelOf(item.key || item.symbol)} (${(item.reason || "insufficient history").replaceAll("_", " ")})`
                )).join(", ")}
              </p>
            )}
            <details className="assumptions-panel">
              <summary>Data window and assumptions</summary>
              <dl className="assumptions-grid">
                <div><dt>Basis</dt><dd>{data.basis || "Not reported"}</dd></div>
                <div><dt>Window</dt><dd>{data.data_window ? `${data.data_window.start} to ${data.data_window.end}` : "Not reported"}</dd></div>
                <div><dt>Observations</dt><dd>{data.observations ?? "Not reported"}</dd></div>
                <div><dt>Risk-free rate</dt><dd>{data.risk_free_rate_annual != null ? fmtPct(data.risk_free_rate_annual * 100) : "Not reported"}</dd></div>
              </dl>
              {data.limitations?.map((limitation) => <p key={limitation} className="muted small">{limitation}</p>)}
            </details>
          </section>

          <section className="card">
            <h3>Allocation</h3>
            <div className="alloc-grid">
              <div
                className="chart-wrap"
                style={{ height: 220, minWidth: 220 }}
                role="img"
                aria-label="Portfolio allocation by asset"
              >
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

          {data.rolling?.length > 0 && (
            <section className="card">
              <h3>Rolling 30-session risk</h3>
              <div style={{ height: 260 }} role="img" aria-label="Rolling Sharpe, volatility, and drawdown">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={data.rolling}>
                    <XAxis dataKey="date" hide />
                    <YAxis />
                    <Tooltip />
                    <Line type="monotone" dataKey="sharpe" stroke="var(--accent)" dot={false} />
                    <Line type="monotone" dataKey="volatility" stroke="var(--green)" dot={false} />
                    <Line type="monotone" dataKey="drawdown" stroke="var(--red)" dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </section>
          )}

          {scopedCorr && scopedCorr.assets.length > 1 && (
            <section className="card">
              <h3>Correlation</h3>
              <p className="muted small">
                Daily-return correlation. Red = moves together, blue = moves opposite.
              </p>
              <CorrHeatmap assets={scopedCorr.assets.map(labelOf)} matrix={scopedCorr.matrix} />
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
            const val = Number.isFinite(v) ? v : null;
            const bg =
              val === null
                ? "var(--panel-2)"
                : val >= 0
                ? `rgba(248,81,73,${Math.min(Math.abs(val), 1) * 0.75})`
                : `rgba(76,154,255,${Math.min(Math.abs(val), 1) * 0.75})`;
            return (
              <span key={j} className="hm-cell" style={{ background: bg }} title={`${a} / ${assets[j]}: ${val === null ? "unavailable" : val.toFixed(2)}`}>
                {val === null ? "N/A" : val.toFixed(2)}
              </span>
            );
          })}
        </div>
      ))}
    </div>
  );
}

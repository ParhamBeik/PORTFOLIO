import { useEffect, useMemo, useState } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  marketAnnouncements,
  marketAssets,
  marketPerformance,
  marketQuota,
  marketShareholders,
} from "../api.js";
import { fmtNum, fmtPct, fmtTomanCompact } from "../format.js";
import ProGate from "./ProGate.jsx";

const WINDOWS = { "1Y": 365, "3Y": 1095, All: Infinity };

export default function MarketData({ user }) {
  const [assets, setAssets] = useState(null);
  const [assetKey, setAssetKey] = useState("");
  const [performance, setPerformance] = useState(null);
  const [quota, setQuota] = useState(null);
  const [windowName, setWindowName] = useState("1Y");
  const [err, setErr] = useState("");

  const refreshQuota = () => {
    marketQuota().then(setQuota).catch(() => {});
  };

  useEffect(() => {
    refreshQuota();
    const interval = setInterval(refreshQuota, 15000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    marketAssets()
      .then((rows) => {
        setAssets(rows);
        if (rows.length) setAssetKey(rows[0].key);
      })
      .catch((error) => setErr(error.message));
  }, []);

  useEffect(() => {
    if (!assetKey) return;
    setPerformance(null);
    setErr("");
    marketPerformance(assetKey)
      .then(setPerformance)
      .catch((error) => setErr(error.message));
  }, [assetKey]);

  const selected = assets?.find((asset) => asset.key === assetKey);
  const series = useMemo(() => {
    const rows = performance?.series || [];
    const size = WINDOWS[windowName];
    const sliced = Number.isFinite(size) ? rows.slice(-size) : rows;
    const base = sliced[0]?.close;
    return sliced.map((row) => ({
      ...row,
      performance: base ? ((Number(row.close) / Number(base)) - 1) * 100 : 0,
    }));
  }, [performance, windowName]);

  if (assets === null && !err) return <p className="muted">Loading market data…</p>;

  const first = series[0];
  const last = series[series.length - 1];
  const closes = series.map((row) => Number(row.close)).filter(Number.isFinite);
  const totalReturn = first?.close
    ? ((Number(last?.close) / Number(first.close)) - 1) * 100
    : null;

  return (
    <div className="dashboard">
      {err && <div className="error">{err}</div>}

      {quota && (
        <section className="card quota-card" style={{ marginBottom: "1.5rem" }}>
          <div className="card-head">
            <h3>⚡ Rate Limit & Backfill Tracking System</h3>
            <button
              className="btn btn-sm"
              onClick={() => {
                refreshQuota();
                marketAssets().then(setAssets);
                if (assetKey) marketPerformance(assetKey).then(setPerformance);
              }}
            >
              🔄 Refresh Status
            </button>
          </div>
          <div className="metric-grid">
            <div className="metric">
              <span className="muted small">Daily Quota Used ({quota.day})</span>
              <strong style={{ fontSize: "1.1rem" }}>
                {fmtNum(quota.used)} / {fmtNum(quota.limit)}
              </strong>
              <span className="muted xsmall">{fmtNum(quota.remaining_daily)} remaining today</span>
            </div>
            <div className="metric">
              <span className="muted small">5-Min Window Quota</span>
              <strong style={{ fontSize: "1.1rem" }}>
                {fmtNum(quota.window_used)} / {fmtNum(quota.window_limit)}
              </strong>
              <span className="muted xsmall">{fmtNum(quota.remaining_window)} remaining in 5m</span>
            </div>
            <div className="metric">
              <span className="muted small">Archive Backfill Coverage</span>
              <strong style={{ fontSize: "1.1rem" }}>
                {fmtNum(quota.archive_progress?.complete_states || 0)} /{" "}
                {fmtNum(quota.archive_progress?.total_states || 0)} states
              </strong>
              <span className="muted xsmall">
                {quota.archive_progress?.progress_pct || 0}% verified complete
              </span>
            </div>
          </div>
        </section>
      )}

      <section className="card">
        <div className="card-head">
          <h2>Market Performance</h2>
          <div className="seg tf-seg">
            {Object.keys(WINDOWS).map((name) => (
              <button
                key={name}
                className={windowName === name ? "active" : ""}
                onClick={() => setWindowName(name)}
              >
                {name}
              </button>
            ))}
          </div>
        </div>

        <div className="inline">
          <select value={assetKey} onChange={(event) => setAssetKey(event.target.value)}>
            {(assets || []).map((asset) => (
              <option key={asset.key} value={asset.key}>
                {asset.name} ({asset.symbol}) — {asset.records} days
              </option>
            ))}
          </select>
        </div>

        {selected && (
          <p className="muted small">
            {selected.source === "stock" ? "Stock" : "Gold"} · {selected.symbol} ·{" "}
            {selected.records} archived days · {selected.first_date || "No data"} to{" "}
            {selected.last_date || "No data"}
          </p>
        )}

        {!performance && !err && <p className="muted small">Loading price history…</p>}
        {performance && series.length === 0 && (
          <p className="muted small">No verified archive rows for this asset yet.</p>
        )}

        {series.length > 0 && (
          <>
            <div className="metric-grid">
              <Metric label="Period return" value={fmtPct(totalReturn)} className={totalReturn >= 0 ? "pos" : "neg"} />
              <Metric label="Latest close" value={fmtNum(last.close)} />
              <Metric label="Period high" value={fmtNum(Math.max(...closes))} />
              <Metric label="Period low" value={fmtNum(Math.min(...closes))} />
              <Metric label="Archived points" value={fmtNum(series.length)} />
              <Metric label="Latest date" value={last.date} />
            </div>

            <div className="chart-wrap market-chart">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={series} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="performance-fill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.45} />
                      <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="date" minTickGap={28} stroke="var(--border)" />
                  <YAxis
                    tickFormatter={(value) => `${Number(value).toFixed(0)}%`}
                    width={52}
                    stroke="var(--border)"
                    domain={["auto", "auto"]}
                  />
                  <Tooltip
                    formatter={(value, name, item) => [
                      `${Number(value).toFixed(2)}% (${fmtTomanCompact(item.payload.close)})`,
                      "Performance",
                    ]}
                    contentStyle={{
                      background: "var(--panel-2)",
                      border: "1px solid var(--border)",
                      borderRadius: 8,
                    }}
                  />
                  <Area
                    type="monotone"
                    dataKey="performance"
                    stroke="var(--accent)"
                    strokeWidth={2}
                    fill="url(#performance-fill)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </>
        )}
      </section>

      {selected?.source === "stock" && (
        <ProGate user={user} pitch="Codal filings and shareholder moves are a Pro feature.">
          <ProSections symbol={selected.symbol} isPro={user?.is_pro} />
        </ProGate>
      )}
    </div>
  );
}

function Metric({ label, value, className = "" }) {
  return (
    <div className="metric">
      <div className={`metric-val ${className}`}>{value}</div>
      <div className="metric-label">{label}</div>
    </div>
  );
}

function ProSections({ symbol, isPro }) {
  const [announcements, setAnnouncements] = useState(null);
  const [holders, setHolders] = useState(null);

  useEffect(() => {
    if (!isPro || !symbol) return;
    setAnnouncements(null);
    setHolders(null);
    marketAnnouncements(symbol, 20).then(setAnnouncements).catch(() => setAnnouncements([]));
    marketShareholders(symbol).then(setHolders).catch(() => setHolders([]));
  }, [isPro, symbol]);

  return (
    <>
      <section className="card">
        <h3>Codal Announcements</h3>
        {!announcements && <p className="muted small">Loading announcements…</p>}
        {announcements?.length === 0 && <p className="muted small">No announcements.</p>}
        {announcements?.length > 0 && (
          <table className="holdings">
            <thead><tr><th>Date</th><th>Title</th><th>Links</th></tr></thead>
            <tbody>
              {announcements.map((announcement, index) => (
                <tr key={index}>
                  <td>{announcement.date_publish}</td>
                  <td>{announcement.title}</td>
                  <td>
                    {announcement.link && <a href={announcement.link} target="_blank" rel="noreferrer">Codal</a>}{" "}
                    {announcement.link_pdf && <a href={announcement.link_pdf} target="_blank" rel="noreferrer">PDF</a>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h3>Major Shareholders</h3>
        {!holders && <p className="muted small">Loading shareholders…</p>}
        {holders?.length === 0 && <p className="muted small">No shareholder data.</p>}
        {holders?.length > 0 && (
          <table className="holdings">
            <thead><tr><th>Shareholder</th><th>Share</th><th>Volume</th><th>Change</th><th>Date</th></tr></thead>
            <tbody>
              {holders.map((holder, index) => (
                <tr key={index}>
                  <td>{holder.name}</td>
                  <td>{fmtPct(holder.percent)}</td>
                  <td>{fmtNum(holder.volume)}</td>
                  <td className={Number(holder.change) >= 0 ? "pos" : "neg"}>{fmtNum(holder.change)}</td>
                  <td>{holder.date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

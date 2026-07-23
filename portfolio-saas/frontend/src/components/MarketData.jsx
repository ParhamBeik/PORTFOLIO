import { useEffect, useMemo, useState } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  marketAnnouncements,
  marketAssets,
  marketPerformance,
  marketShareholders,
} from "../api.js";
import { fmtNum, fmtPct, fmtTomanCompact } from "../format.js";
import ProGate from "./ProGate.jsx";

const WINDOWS = { "1Y": 365, "3Y": 1095, All: Infinity };
const ASSET_CLASSES = ["Currency", "Gold", "Stock"];

const sourceLabel = (asset) => {
  if (asset?.source === "stock") return "Stock";
  if (asset?.source === "currency") return "Currency";
  return "Gold";
};

const classOf = (asset) => sourceLabel(asset);
const aliasOf = (asset) => asset.symbol || asset.name;

export default function MarketData({ user }) {
  const [assets, setAssets] = useState(null);
  const [assetKey, setAssetKey] = useState("");
  const [selectedSector, setSelectedSector] = useState("ALL");
  const [searchQuery, setSearchQuery] = useState("");
  const [performance, setPerformance] = useState(null);
  const [windowName, setWindowName] = useState("1Y");
  const [activeClass, setActiveClass] = useState("Currency");
  const [err, setErr] = useState("");

  useEffect(() => {
    marketAssets()
      .then((rows) => {
        setAssets(rows);
        const first = rows.find((asset) => classOf(asset) === "Currency") || rows[0];
        if (first) setAssetKey(first.key);
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

  // Group assets by Class -> Sector -> Asset
  const groupedAssets = useMemo(() => {
    const groups = {};
    for (const asset of assets || []) {
      const cls = classOf(asset);
      const sector = cls === "Stock" ? asset.sector || "Unclassified" : cls;
      groups[cls] ??= {};
      groups[cls][sector] ??= [];
      groups[cls][sector].push(asset);
    }
    return groups;
  }, [assets]);

  // Available sectors for the current active class
  const availableSectors = useMemo(() => {
    if (activeClass !== "Stock") return [];
    const sectors = Object.keys(groupedAssets["Stock"] || {}).sort();
    return sectors;
  }, [groupedAssets, activeClass]);

  // Filtered assets based on selected sector and search query
  const filteredAssets = useMemo(() => {
    const classGroups = groupedAssets[activeClass] || {};
    let list = (activeClass !== "Stock" || selectedSector === "ALL")
      ? Object.values(classGroups).flat()
      : classGroups[selectedSector] || [];
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim();
      list = list.filter(
        (a) =>
          (a.symbol && a.symbol.toLowerCase().includes(q)) ||
          (a.name && a.name.toLowerCase().includes(q)) ||
          (a.key && a.key.toLowerCase().includes(q))
      );
    }
    return list;
  }, [groupedAssets, activeClass, selectedSector, searchQuery]);

  // Update selected asset when sector changes
  useEffect(() => {
    if (filteredAssets.length > 0) {
      if (!filteredAssets.some((a) => a.key === assetKey)) {
        setAssetKey(filteredAssets[0].key);
      }
    }
  }, [filteredAssets, assetKey]);

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

  const returns = useMemo(() => {
    const values = [];
    for (let i = 1; i < series.length; i += 1) {
      const prev = Number(series[i - 1].close);
      const next = Number(series[i].close);
      if (prev > 0 && Number.isFinite(next)) values.push(next / prev - 1);
    }
    return values;
  }, [series]);

  if (assets === null && !err) return <p className="muted">Loading market data…</p>;

  const first = series[0];
  const last = series[series.length - 1];
  const closes = series.map((row) => Number(row.close)).filter(Number.isFinite);
  const totalReturn = first?.close
    ? ((Number(last?.close) / Number(first.close)) - 1) * 100
    : null;
  const avg = returns.length ? returns.reduce((sum, value) => sum + value, 0) / returns.length : 0;
  const variance = returns.length
    ? returns.reduce((sum, value) => sum + ((value - avg) ** 2), 0) / returns.length
    : 0;
  const expectedAnnual = avg * 252 * 100;
  const volatilityAnnual = Math.sqrt(variance) * Math.sqrt(252) * 100;

  return (
    <div className="dashboard">
      {err && <div className="error">{err}</div>}

      <section className="card market-card">
        <div className="card-head">
          <div className="market-header-title">
            <h2>Market Explorer</h2>
            <span className="muted small">Analyze performance across Iranian markets</span>
          </div>
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

        {/* Asset Class Switcher */}
        <div className="seg market-class-tabs">
          {ASSET_CLASSES.map((name) => (
            <button
              key={name}
              type="button"
              className={activeClass === name ? "active" : ""}
              onClick={() => {
                setActiveClass(name);
                setSelectedSector("ALL");
                const first = (assets || []).find((asset) => classOf(asset) === name);
                if (first) setAssetKey(first.key);
              }}
            >
              {name === "Stock" ? "📈 Stocks" : name === "Gold" ? "🥇 Gold" : "💵 Currency"}
            </button>
          ))}
        </div>

        {/* Hierarchical Filter Bar */}
        <div className="market-controls-panel">
          <div className="control-group" style={{ minWidth: "180px" }}>
            <label htmlFor="search-input" className="control-label">
              Search Symbol
            </label>
            <input
              id="search-input"
              type="text"
              className="market-select"
              placeholder="Filter by ticker/name..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>

          {activeClass === "Stock" && (
            <div className="control-group">
              <label htmlFor="sector-select" className="control-label">
                Sector / Industry
              </label>
              <select
                id="sector-select"
                className="market-select"
                value={selectedSector}
                onChange={(e) => setSelectedSector(e.target.value)}
              >
                <option value="ALL">All Sectors ({availableSectors.length})</option>
                {availableSectors.map((sector) => (
                  <option key={sector} value={sector}>
                    {sector} ({(groupedAssets["Stock"][sector] || []).length} assets)
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="control-group flex-1">
            <label htmlFor="asset-select" className="control-label">
              Select Asset
            </label>
            <select
              id="asset-select"
              className="market-select"
              value={assetKey}
              onChange={(e) => setAssetKey(e.target.value)}
            >
              {filteredAssets.map((asset) => (
                <option key={asset.key} value={asset.key}>
                  {activeClass === "Stock"
                    ? `${aliasOf(asset)} — ${asset.name} (${asset.sector || "Unclassified"})`
                    : `${asset.name} (${asset.symbol})`}
                  {asset.records ? ` · ${asset.records} archived days` : ""}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Active Asset Metadata Banner */}
        {selected && (
          <div className="market-asset-banner">
            <div className="asset-main-info">
              <div className="asset-title-row">
                <span className="asset-symbol-badge">{aliasOf(selected)}</span>
                <h3 className="asset-fullname">{selected.name}</h3>
              </div>
              <div className="asset-meta-tags">
                <span className="badge">{sourceLabel(selected)}</span>
                {selected.sector && <span className="badge sector-badge">{selected.sector}</span>}
                {selected.sector_sub && <span className="badge sub-badge">{selected.sector_sub}</span>}
                <span className="badge muted-badge">
                  {selected.records} archived days ({selected.first_date || "N/A"} to {selected.last_date || "N/A"})
                </span>
              </div>
            </div>
            {(selected.pe != null || selected.eps != null || selected.market_cap != null) && (
              <div
                className="asset-fundamentals-grid"
                style={{
                  display: "flex",
                  gap: "20px",
                  marginTop: "12px",
                  paddingTop: "10px",
                  borderTop: "1px solid var(--border)",
                  fontSize: "13px",
                }}
              >
                {selected.pe != null && (
                  <div>
                    <span className="muted">P/E Ratio: </span>
                    <strong style={{ color: "var(--accent)" }}>{fmtNum(selected.pe)}</strong>
                  </div>
                )}
                {selected.eps != null && (
                  <div>
                    <span className="muted">EPS: </span>
                    <strong>{fmtTomanCompact(selected.eps)} T</strong>
                  </div>
                )}
                {selected.market_cap != null && (
                  <div>
                    <span className="muted">Market Cap: </span>
                    <strong>{fmtTomanCompact(selected.market_cap)} T</strong>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Metrics Grid */}
        {series.length > 0 && (
          <>
            <div className="metric-grid">
              <Metric label="Period return" value={fmtPct(totalReturn)} className={totalReturn >= 0 ? "pos" : "neg"} />
              <Metric label="Expected return (Ann.)" value={fmtPct(expectedAnnual)} className={expectedAnnual >= 0 ? "pos" : "neg"} />
              <Metric label="Volatility (Ann.)" value={fmtPct(volatilityAnnual)} />
              <Metric label="Latest close" value={fmtTomanCompact(last.close)} />
              <Metric label="Period high" value={fmtTomanCompact(Math.max(...closes))} />
              <Metric label="Period low" value={fmtTomanCompact(Math.min(...closes))} />
              <Metric label="Archived points" value={fmtNum(series.length)} />
              <Metric label="Latest date" value={last.date} />
            </div>

            {/* Performance Chart */}
            <div className="market-chart-section">
              <div className="chart-head-info">
                <h4>Price & Return Trend ({windowName})</h4>
              </div>
              <div className="chart-wrap market-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={series} margin={{ top: 12, right: 16, left: 0, bottom: 0 }}>
                    <defs>
                      <linearGradient id="performance-fill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.45} />
                        <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <XAxis dataKey="date" minTickGap={32} stroke="var(--border)" />
                    <YAxis
                      tickFormatter={(value) => `${Number(value).toFixed(0)}%`}
                      width={56}
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
                        boxShadow: "0 4px 16px rgba(0,0,0,0.3)",
                      }}
                    />
                    <Area
                      type="monotone"
                      dataKey="performance"
                      stroke="var(--accent)"
                      strokeWidth={2.5}
                      fill="url(#performance-fill)"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>
          </>
        )}

        {/* Sector Comparison Benchmarks */}
        {filteredAssets.length > 1 && (
          <div className="market-comparison-section">
            <div className="comparison-header">
              <h4>
                {activeClass === "Stock" && selectedSector !== "ALL"
                  ? `Peer Assets in ${selectedSector}`
                  : `Top Assets in ${activeClass}`}
              </h4>
              <span className="muted small">Quick overview of comparative archive depth</span>
            </div>
            <div className="comparison-grid">
              {filteredAssets
                .filter((asset) => asset.key !== assetKey)
                .slice(0, 8)
                .map((asset) => (
                  <button
                    type="button"
                    key={asset.key}
                    className="comparison-card"
                    onClick={() => setAssetKey(asset.key)}
                  >
                    <div className="comp-symbol">{aliasOf(asset)}</div>
                    <div className="comp-name">{asset.name}</div>
                    <div className="comp-meta">{asset.records} archived days</div>
                  </button>
                ))}
            </div>
          </div>
        )}

        {!performance && !err && <p className="muted small">Loading price history…</p>}
        {performance && series.length === 0 && (
          <p className="muted small">No verified archive rows for this asset yet.</p>
        )}
      </section>

      {/* Pro Filings & Shareholder Data */}
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
    <div className="pro-sections-grid">
      <section className="card">
        <div className="card-head">
          <h3>Codal Announcements</h3>
          <span className="badge pro-badge">Pro</span>
        </div>
        {!announcements && <p className="muted small">Loading announcements…</p>}
        {announcements?.length === 0 && <p className="muted small">No announcements found.</p>}
        {announcements?.length > 0 && (
          <table className="holdings font-small">
            <thead>
              <tr>
                <th>Date</th>
                <th>Title</th>
                <th>Links</th>
              </tr>
            </thead>
            <tbody>
              {announcements.map((announcement, index) => (
                <tr key={index}>
                  <td>{announcement.date_publish}</td>
                  <td>
                    {announcement.category_title && (
                      <span className="badge muted-badge" style={{ marginRight: 6 }}>
                        {announcement.category_title}
                      </span>
                    )}
                    {announcement.title}
                  </td>
                  <td>
                    {announcement.link && (
                      <a href={announcement.link} target="_blank" rel="noreferrer" className="link-btn">
                        Codal
                      </a>
                    )}{" "}
                    {announcement.link_pdf && (
                      <a href={announcement.link_pdf} target="_blank" rel="noreferrer" className="link-btn">
                        PDF
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h3>Major Shareholders</h3>
          <span className="badge pro-badge">Pro</span>
        </div>
        {!holders && <p className="muted small">Loading shareholders…</p>}
        {holders?.length === 0 && <p className="muted small">No shareholder data.</p>}
        {holders?.length > 0 && (
          <table className="holdings font-small">
            <thead>
              <tr>
                <th>Shareholder</th>
                <th>Share %</th>
                <th>Volume</th>
                <th>Change</th>
                <th>Date</th>
              </tr>
            </thead>
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
    </div>
  );
}

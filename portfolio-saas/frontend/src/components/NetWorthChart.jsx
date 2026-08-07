import { useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { snapshots } from "../api.js";
import { fmtChartTooltipDate, fmtDateTick, fmtTomanCompact } from "../format.js";

export default function NetWorthChart({
  days = 7,
  onDaysChange,
  account = null,
  initialCurrency = "TMN",
}) {
  const [data, setData] = useState(null);
  const [trades, setTrades] = useState([]);
  const [currency, setCurrency] = useState(initialCurrency);
  const [err, setErr] = useState("");
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let current = true;
    snapshots(days, account)
      .then((res) => {
        if (!current) return;
        const series = Array.isArray(res) ? res : res.series || [];
        const markers = Array.isArray(res) ? [] : res.trades || [];
        const parsed = series
          .map((r) => {
            const ts = new Date(r.timestamp).getTime();
            if (isNaN(ts)) return null;
            return {
              ts,
              dateStr: r.date || new Date(r.timestamp).toISOString().split("T")[0],
              total: Number(r.total),
              total_usd: r.total_usd != null ? Number(r.total_usd) : null,
              isEstimated: !!r.is_estimated,
            };
          })
          .filter(Boolean);
        setData(parsed);
        setTrades(markers);
        setErr("");
      })
      .catch((e) => {
        if (current) setErr(e.message);
      });
    return () => {
      current = false;
    };
  }, [days, account, retryKey]);

  if (err) {
    return (
      <div className="error inline" role="alert">
        <span>History chart unavailable: {err}</span>
        <button type="button" className="link" onClick={() => setRetryKey((key) => key + 1)}>
          Retry
        </button>
      </div>
    );
  }
  if (data === null) return <p className="muted small">Loading history chart…</p>;
  if (!data.length)
    return (
      <p className="muted small">
        Net-worth history builds up as prices are fetched (every few minutes).
      </p>
    );

  const isUsd = currency === "USD";
  const dataKey = isUsd ? "total_usd" : "total";
  const displayData = data.filter((point) => Number.isFinite(point[dataKey]));

  // Calculate period statistics only from values available in the selected basis.
  const firstVal = displayData[0]?.[dataKey] ?? 0;
  const lastVal = displayData[displayData.length - 1]?.[dataKey] ?? 0;
  const changeAmt = lastVal - firstVal;
  const pctChange = firstVal > 0 ? (changeAmt / firstVal) * 100 : 0;
  const isPositive = changeAmt >= 0;

  const totals = displayData.map((point) => point[dataKey]);
  const min = totals.length ? Math.min(...totals) : 0;
  const max = totals.length ? Math.max(...totals) : 0;
  const range = max - min;
  const pad = range === 0 ? (min === 0 ? 100 : Math.abs(min) * 0.05) : range * 0.05;
  const yMin = min >= 0 ? Math.max(0, min - pad) : min - pad;
  const yMax = max + pad;
  const yDomain = [yMin, yMax];

  const formatValue = (v) =>
    isUsd
      ? `$${Number(v).toLocaleString("en-US", { maximumFractionDigits: 1 })}`
      : `${fmtTomanCompact(v)} T`;

  // Snap trade markers to nearest snapshot line point
  const nearestTotal = (tradeTs) => {
    let best = displayData[0];
    for (const p of displayData) {
      if (Math.abs(p.ts - tradeTs) < Math.abs(best.ts - tradeTs)) best = p;
    }
    return best;
  };

  return (
    <div className="chart-container">
      {/* Top Header Panel: Title + Period Stats + Separated Controls */}
      <div className="chart-header-panel">
        <div className="chart-title-box">
          <div className="chart-stats-row">
            <span>Historical value change:</span>
            <span className={`stat-pill ${isPositive ? "pos" : "neg"}`}>
              {isPositive ? "▲ +" : "▼ "}
              {pctChange.toFixed(2)}% ({formatValue(Math.abs(changeAmt))})
            </span>
            <span className="muted">Range: {formatValue(min)} — {formatValue(max)}</span>
          </div>
        </div>

        {/* Separated Toolbar Controls: Timeframes & Currency */}
        <div className="chart-controls-wrap">
          {/* Timeframe Control Group */}
          <div className="control-group" role="group" aria-label="History range">
            <span className="control-label">Range</span>
            {[
              { d: 7, label: "7D" },
              { d: 30, label: "30D" },
              { d: 90, label: "90D" },
              { d: "all", label: "ALL" },
            ].map(({ d, label }) => (
              <button
                key={d}
                type="button"
                className={days === d ? "active" : ""}
                aria-pressed={days === d}
                onClick={() => onDaysChange && onDaysChange(d)}
              >
                {label}
              </button>
            ))}
          </div>

          {/* Currency Control Group */}
          <div className="control-group" role="group" aria-label="Valuation basis">
            <span className="control-label">Valuation basis</span>
            <button
              type="button"
              className={currency === "TMN" ? "active" : ""}
              aria-pressed={currency === "TMN"}
              onClick={() => setCurrency("TMN")}
            >
              IRT (TMN)
            </button>
            <button
              type="button"
              className={currency === "USD" ? "active" : ""}
              aria-pressed={currency === "USD"}
              onClick={() => setCurrency("USD")}
            >
              USD-denominated ($)
            </button>
          </div>
        </div>
      </div>

      {displayData.length === 0 ? (
        <p className="muted small" role="status">
          USD-denominated history is unavailable for this range; no Toman values were substituted.
        </p>
      ) : <div
        className="chart-wrap"
        style={{ height: 260 }}
        role="img"
        aria-label={`Historical net worth for the selected ${days}-day range in ${isUsd ? "USD-denominated" : "nominal Toman"} values`}
      >
        <ResponsiveContainer width="100%" height="100%" minWidth={100} minHeight={200}>
          <AreaChart data={displayData} margin={{ top: 12, right: 16, left: 4, bottom: 4 }}>
            <defs>
              <linearGradient id="nw-fill-v2" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={isPositive ? "var(--accent)" : "var(--amber)"} stopOpacity={0.4} />
                <stop offset="50%" stopColor={isPositive ? "var(--accent)" : "var(--amber)"} stopOpacity={0.12} />
                <stop offset="100%" stopColor={isPositive ? "var(--accent)" : "var(--amber)"} stopOpacity={0.0} />
              </linearGradient>
            </defs>
            <XAxis
              dataKey="ts"
              type="number"
              domain={["dataMin", "dataMax"]}
              scale="time"
              tickFormatter={(t) => fmtDateTick(t, days)}
              tick={{ fontSize: 11, fill: "var(--muted)" }}
              minTickGap={28}
              stroke="var(--border)"
            />
            <YAxis
              domain={yDomain}
              tickFormatter={(v) => (isUsd ? `$${Math.round(v)}` : fmtTomanCompact(v))}
              tick={{ fontSize: 11, fill: "var(--muted)" }}
              width={64}
              stroke="var(--border)"
            />
            <Tooltip
              labelFormatter={(t) => fmtChartTooltipDate(t)}
              formatter={(v, name, props) => {
                const isEst = props.payload?.isEstimated;
                const formatted = formatValue(v);
                return [isEst ? `${formatted} (Estimated)` : formatted, "Net Worth"];
              }}
              contentStyle={{
                background: "var(--panel)",
                border: "1px solid var(--border)",
                borderRadius: 12,
                boxShadow: "0 10px 25px rgba(0,0,0,0.3)",
                color: "var(--text)",
                padding: "8px 12px",
              }}
              labelStyle={{ color: "var(--muted)", fontWeight: 600, fontSize: "12px", marginBottom: "4px" }}
            />
            <Area
              type="monotone"
              dataKey={dataKey}
              stroke={isPositive ? "var(--accent)" : "var(--amber)"}
              strokeWidth={2.5}
              fill="url(#nw-fill-v2)"
              dot={{ r: 3, strokeWidth: 1, fill: isPositive ? "var(--accent)" : "var(--amber)", stroke: "var(--panel)" }}
              activeDot={{ r: 6, fill: "var(--accent)", stroke: "var(--panel)", strokeWidth: 2 }}
            />
            {trades.map((t, i) => {
              const ts = new Date(t.timestamp).getTime();
              if (isNaN(ts)) return null;
              const point = nearestTotal(ts);
              if (!point) return null;
              const color = t.side === "buy" ? "var(--green)" : "var(--red)";
              const yVal = point[dataKey];
              return (
                <ReferenceDot
                  key={i}
                  x={point.ts}
                  y={yVal}
                  r={5}
                  fill={color}
                  stroke="var(--panel)"
                  strokeWidth={2}
                  isFront
                />
              );
            })}
          </AreaChart>
        </ResponsiveContainer>
      </div>}
    </div>
  );
}

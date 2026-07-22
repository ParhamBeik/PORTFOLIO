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
import { fmtTehranTime, fmtTomanCompact } from "../format.js";

// FREE: net-worth trend from the snapshot series (cron after each price fetch,
// plus one per trade). Buy/sell events are overlaid as dots so you can see where
// holdings changed vs. where the market moved. One small GET, no Pro dependency.
export default function NetWorthChart({ days = 7, account = null, initialCurrency = "TMN" }) {
  const [data, setData] = useState(null);
  const [trades, setTrades] = useState([]);
  const [currency, setCurrency] = useState(initialCurrency);
  const [err, setErr] = useState("");

  useEffect(() => {
    let current = true;
    snapshots(days, account)
      .then((res) => {
        if (!current) return;
        const series = Array.isArray(res) ? res : res.series || [];
        const markers = Array.isArray(res) ? [] : res.trades || [];
        setData(
          series.map((r) => ({
            ts: new Date(r.timestamp).getTime(),
            total: Number(r.total),
            total_usd: r.total_usd != null ? Number(r.total_usd) : null,
          }))
        );
        setTrades(markers);
        setErr("");
      })
      .catch((e) => { if (current) setErr(e.message); });
    return () => { current = false; };
  }, [days, account]);

  if (err) return <p className="muted small">{err}</p>;
  if (data === null) return <p className="muted small">Loading history…</p>;
  if (!data.length)
    return (
      <p className="muted small">
        Net-worth history builds up as prices are fetched (every few minutes).
      </p>
    );

  const isUsd = currency === "USD";
  const dataKey = isUsd ? "total_usd" : "total";

  // Snap each trade to the nearest snapshot point so the dot sits on the line.
  const nearestTotal = (tradeTs) => {
    let best = data[0];
    for (const p of data) {
      if (Math.abs(p.ts - tradeTs) < Math.abs(best.ts - tradeTs)) best = p;
    }
    return best;
  };

  const totals = data.map((d) => (isUsd ? d.total_usd ?? d.total : d.total)).filter((v) => v !== null);
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

  return (
    <div className="chart-container">
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 6 }}>
        <div className="seg tf-seg" style={{ fontSize: "12px" }}>
          <button
            type="button"
            className={currency === "TMN" ? "active" : ""}
            onClick={() => setCurrency("TMN")}
          >
            Tomans (TMN)
          </button>
          <button
            type="button"
            className={currency === "USD" ? "active" : ""}
            onClick={() => setCurrency("USD")}
          >
            USD ($)
          </button>
        </div>
      </div>
      <div className="chart-wrap" style={{ height: 220 }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="nw-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.45} />
                <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <XAxis
              dataKey="ts"
              type="number"
              domain={["dataMin", "dataMax"]}
              scale="time"
              tickFormatter={(t) => fmtTehranTime(new Date(t).toISOString())}
              tick={{ fontSize: 11, fill: "var(--muted)" }}
              minTickGap={28}
              stroke="var(--border)"
            />
            <YAxis
              domain={yDomain}
              tickFormatter={(v) => (isUsd ? `$${Math.round(v)}` : fmtTomanCompact(v))}
              tick={{ fontSize: 11, fill: "var(--muted)" }}
              width={54}
              stroke="var(--border)"
            />
            <Tooltip
              labelFormatter={(t) => fmtTehranTime(new Date(t).toISOString())}
              formatter={(v) => [formatValue(v), "Net worth"]}
              contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 8 }}
              labelStyle={{ color: "var(--muted)" }}
            />
            <Area type="monotone" dataKey={dataKey} stroke="var(--accent)" strokeWidth={2} fill="url(#nw-fill)" />
            {trades.map((t, i) => {
              const ts = new Date(t.timestamp).getTime();
              const point = nearestTotal(ts);
              const color = t.side === "buy" ? "#22c55e" : "#ef4444";
              const yVal = isUsd ? point.total_usd ?? point.total : point.total;
              return (
                <ReferenceDot
                  key={i}
                  x={point.ts}
                  y={yVal}
                  r={5}
                  fill={color}
                  stroke="var(--panel-2)"
                  strokeWidth={2}
                  isFront
                />
              );
            })}
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

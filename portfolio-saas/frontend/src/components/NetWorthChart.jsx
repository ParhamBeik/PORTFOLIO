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
export default function NetWorthChart({ days = 30, account = null }) {
  const [data, setData] = useState(null);
  const [trades, setTrades] = useState([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    let current = true;
    snapshots(days, account)
      .then((res) => {
        if (!current) return;
        // Backend returns { series, trades }. Guard against the old bare-array
        // shape so a stale cache/older backend still renders the line.
        const series = Array.isArray(res) ? res : res.series || [];
        const markers = Array.isArray(res) ? [] : res.trades || [];
        setData(
          series.map((r) => ({
            ts: new Date(r.timestamp).getTime(),
            total: Number(r.total),
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

  // Snap each trade to the nearest snapshot point so the dot sits on the line.
  const nearestTotal = (tradeTs) => {
    let best = data[0];
    for (const p of data) {
      if (Math.abs(p.ts - tradeTs) < Math.abs(best.ts - tradeTs)) best = p;
    }
    return best;
  };

  return (
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
            tickFormatter={fmtTomanCompact}
            tick={{ fontSize: 11, fill: "var(--muted)" }}
            width={48}
            stroke="var(--border)"
          />
          <Tooltip
            labelFormatter={(t) => fmtTehranTime(new Date(t).toISOString())}
            formatter={(v) => [fmtTomanCompact(v) + " T", "Net worth"]}
            contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 8 }}
            labelStyle={{ color: "var(--muted)" }}
          />
          <Area type="monotone" dataKey="total" stroke="var(--accent)" strokeWidth={2} fill="url(#nw-fill)" />
          {trades.map((t, i) => {
            const ts = new Date(t.timestamp).getTime();
            const point = nearestTotal(ts);
            // Buy = green, sell = red.
            const color = t.side === "buy" ? "#22c55e" : "#ef4444";
            return (
              <ReferenceDot
                key={i}
                x={point.ts}
                y={point.total}
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
  );
}

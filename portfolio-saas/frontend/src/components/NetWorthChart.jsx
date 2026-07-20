import { useEffect, useState } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { snapshots } from "../api.js";
import { fmtTehranTime, fmtTomanCompact } from "../format.js";

// FREE: net-worth trend from the snapshot series the cron writes after each
// price fetch. Stays light — one small GET, no Pro dependency.
export default function NetWorthChart({ days = 30 }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    snapshots(days)
      .then((rows) => {
        setData(
          rows.map((r) => ({
            ts: new Date(r.timestamp).getTime(),
            total: Number(r.total),
          }))
        );
        setErr("");
      })
      .catch((e) => setErr(e.message));
  }, [days]);

  if (err) return <p className="muted small">{err}</p>;
  if (data === null) return <p className="muted small">Loading history…</p>;
  if (!data.length)
    return (
      <p className="muted small">
        Net-worth history builds up as prices are fetched (every few minutes).
      </p>
    );

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
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

import { useEffect, useState } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  marketAnnouncements,
  marketHistory,
  marketIndex,
  marketShareholders,
  marketSymbols,
} from "../api.js";
import { fmtNum, fmtPct, fmtTomanCompact } from "../format.js";
import ProGate from "./ProGate.jsx";

// FREE: TSE market data. Symbol prices + overall index for everyone;
// Codal announcements & shareholder moves sit behind ProGate.
export default function MarketData({ user }) {
  const [symbols, setSymbols] = useState(null);
  const [symbol, setSymbol] = useState("");
  const [history, setHistory] = useState(null);
  const [index, setIndex] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    marketSymbols()
      .then((rows) => {
        setSymbols(rows);
        if (rows.length && !symbol) setSymbol(rows[0].symbol);
      })
      .catch((e) => setErr(e.message));
    marketIndex(365).then(setIndex).catch(() => {});
  }, []);

  useEffect(() => {
    if (!symbol) return;
    setHistory(null);
    marketHistory(symbol, { adjusted: 1, limit: 365 })
      .then((rows) => setHistory(rows.map((r) => ({ ...r, close: Number(r.close) }))))
      .catch((e) => setErr(e.message));
  }, [symbol]);

  if (symbols === null && !err) return <p className="muted">Loading market data…</p>;

  if (symbols !== null && symbols.length === 0) {
    return (
      <div className="dashboard">
        <section className="card">
          <h2>Market</h2>
          <p className="muted">
            No market data yet — run manage.py backfill_market_data
          </p>
        </section>
      </div>
    );
  }

  const selected = symbols?.find((s) => s.symbol === symbol);
  const indexData = (index || []).map((r) => ({
    date: r.date,
    index_overall: Number(r.index_overall),
  }));

  return (
    <div className="dashboard">
      {err && <div className="error">{err}</div>}

      <section className="card">
        <h2>Market</h2>
        <div className="inline">
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {(symbols || []).map((s) => (
              <option key={s.symbol} value={s.symbol}>
                {s.symbol} — {s.name}
              </option>
            ))}
          </select>
        </div>
        {selected && (
          <p className="muted small">
            {selected.sector} · {selected.market} · EPS {fmtNum(selected.eps)} · P/E{" "}
            {fmtNum(selected.pe)} · Mkt cap {fmtTomanCompact(selected.market_cap)}
          </p>
        )}
        {!history && <p className="muted small">Loading price history…</p>}
        {history && history.length === 0 && (
          <p className="muted small">No price history for this symbol yet.</p>
        )}
        {history && history.length > 0 && (
          <div className="chart-wrap" style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={history} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="mk-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 11, fill: "var(--muted)" }}
                  minTickGap={28}
                  stroke="var(--border)"
                />
                <YAxis
                  tickFormatter={fmtTomanCompact}
                  tick={{ fontSize: 11, fill: "var(--muted)" }}
                  width={48}
                  stroke="var(--border)"
                  domain={["auto", "auto"]}
                />
                <Tooltip
                  formatter={(v) => [fmtNum(v), "Close"]}
                  contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 8 }}
                  labelStyle={{ color: "var(--muted)" }}
                />
                <Area type="monotone" dataKey="close" stroke="var(--accent)" strokeWidth={2} fill="url(#mk-fill)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </section>

      {indexData.length > 0 && (
        <section className="card">
          <h3>TSE overall index</h3>
          <div className="chart-wrap" style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={indexData} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="idx-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--gold)" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="var(--gold)" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 11, fill: "var(--muted)" }}
                  minTickGap={28}
                  stroke="var(--border)"
                />
                <YAxis
                  tickFormatter={fmtTomanCompact}
                  tick={{ fontSize: 11, fill: "var(--muted)" }}
                  width={48}
                  stroke="var(--border)"
                  domain={["auto", "auto"]}
                />
                <Tooltip
                  formatter={(v) => [fmtNum(v), "Index"]}
                  contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 8 }}
                  labelStyle={{ color: "var(--muted)" }}
                />
                <Area type="monotone" dataKey="index_overall" stroke="var(--gold)" strokeWidth={2} fill="url(#idx-fill)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </section>
      )}

      <ProGate user={user} pitch="Codal filings and shareholder moves are a Pro feature.">
        <ProSections symbol={symbol} isPro={user?.is_pro} />
      </ProGate>
    </div>
  );
}

// Pro-only fetches live below the gate so free users never trigger the 403s.
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
        <h3>Codal announcements</h3>
        {!announcements && <p className="muted small">Loading announcements…</p>}
        {announcements && announcements.length === 0 && (
          <p className="muted small">No announcements for this symbol.</p>
        )}
        {announcements && announcements.length > 0 && (
          <table className="holdings">
            <thead>
              <tr><th>Date</th><th>Title</th><th>Links</th></tr>
            </thead>
            <tbody>
              {announcements.map((a, i) => (
                <tr key={i}>
                  <td>
                    {a.date_publish}
                    {a.time_publish && <span className="muted small"> {a.time_publish}</span>}
                  </td>
                  <td>{a.title}</td>
                  <td>
                    {a.link && (
                      <a href={a.link} target="_blank" rel="noreferrer">Codal</a>
                    )}{" "}
                    {a.link_pdf && (
                      <a href={a.link_pdf} target="_blank" rel="noreferrer">PDF</a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h3>Major shareholders</h3>
        {!holders && <p className="muted small">Loading shareholders…</p>}
        {holders && holders.length === 0 && (
          <p className="muted small">No shareholder data for this symbol.</p>
        )}
        {holders && holders.length > 0 && (
          <table className="holdings">
            <thead>
              <tr><th>Shareholder</th><th>Share</th><th>Volume</th><th>Change</th><th>Date</th></tr>
            </thead>
            <tbody>
              {holders.map((h, i) => (
                <tr key={i}>
                  <td>{h.name}</td>
                  <td>{fmtPct(h.percent)}</td>
                  <td>{fmtNum(h.volume)}</td>
                  <td className={Number(h.change) >= 0 ? "pos" : "neg"}>{fmtNum(h.change)}</td>
                  <td>{h.date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

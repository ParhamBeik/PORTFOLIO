import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { accountValuation, priceHistory } from "../api.js";
import Sparkline from "./Sparkline.jsx";
import { fmtNum, fmtToman, fmtTehranTime } from "../format.js";

export default function AccountDetail({ user }) {
  const { id } = useParams();
  const [acct, setAcct] = useState(null);
  const [err, setErr] = useState("");
  // Price-trend sparkline for one of this account's holdings.
  const [trendKey, setTrendKey] = useState("");
  const [hist, setHist] = useState(null); // { points: number[], last: ISO|null }

  useEffect(() => {
    setAcct(null);
    setErr("");
    accountValuation(id).then(setAcct).catch((e) => setErr(e.message));
  }, [id]);

  // Default the trend to the account's first holding once it loads.
  useEffect(() => {
    if (acct && acct.items?.length && !trendKey) setTrendKey(acct.items[0].key);
  }, [acct, trendKey]);

  useEffect(() => {
    if (!trendKey) { setHist(null); return; }
    let cancelled = false;
    priceHistory(trendKey)
      .then((rows) => {
        if (cancelled) return;
        // PriceHistoryView returns newest-first; reverse for a left-to-right trend.
        const ordered = [...rows].reverse();
        setHist({
          points: ordered.map((r) => r.price),
          last: ordered.length ? ordered[ordered.length - 1].fetched_at : null,
        });
      })
      .catch(() => !cancelled && setHist(null));
    return () => { cancelled = true; };
  }, [trendKey]);

  if (err) return <div className="error">{err}</div>;
  if (!acct) return <p className="muted">Loading…</p>;

  return (
    <div className="dashboard">
      <section className="hero">
        <div>
          <div className="hero-label">{acct.name}</div>
          <div className="hero-value">{fmtToman(acct.total)}</div>
          <div className="hero-sub">≈ ${fmtNum(acct.total_usd)} USD</div>
        </div>
        <Link to="/dashboard" className="hero-meta">← All accounts</Link>
      </section>

      <section className="card">
        <h2>Holdings</h2>
        {acct.items.length === 0 ? (
          <p className="muted">No holdings in this account.</p>
        ) : (
          <table className="holdings">
            <thead>
              <tr><th>Asset</th><th>Class</th><th>Qty</th><th>Unit (T)</th><th>Value (T)</th></tr>
            </thead>
            <tbody>
              {acct.items.map((it) => (
                <tr key={it.key}>
                  <td>{it.asset}</td>
                  <td>{it.class}</td>
                  <td>{fmtNum(it.quantity)}</td>
                  <td>{fmtNum(it.unit_price)}</td>
                  <td>{fmtNum(it.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {acct.items.length > 0 && (
        <section className="card">
          <h2>Price trend</h2>
          <div className="inline">
            <select value={trendKey} onChange={(e) => setTrendKey(e.target.value)}>
              {acct.items.map((it) => (
                <option key={it.key} value={it.key}>{it.asset}</option>
              ))}
            </select>
            {hist?.last && <span className="muted small">last tick {fmtTehranTime(hist.last)}</span>}
          </div>
          <Sparkline data={hist?.points} />
          <p className="muted small">
            Recent unit prices (Tomans) from the shared price feed.
            {!user?.is_pro && " Upgrade to Pro for deeper trend analytics."}
          </p>
        </section>
      )}
    </div>
  );
}

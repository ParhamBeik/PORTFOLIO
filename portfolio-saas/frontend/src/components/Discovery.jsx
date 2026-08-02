import { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Discovery() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [watchlistError, setWatchlistError] = useState("");
  const [updateError, setUpdateError] = useState("");
  const [watchlist, setWatchlist] = useState({ items: [] });
  const [basis, setBasis] = useState("nominal_toman");
  const [metric, setMetric] = useState("sharpe"); // sharpe, sortino, calmar
  const [newItem, setNewItem] = useState("");
  const [updating, setUpdating] = useState(false);

  const fetchDiscovery = async () => {
    try {
      setLoading(true);
      setError("");
      const res = await api(`/api/discovery/`);
      setData(res);
    } catch (err) {
      setError(err.message || "Failed to load discovery data.");
    } finally {
      setLoading(false);
    }
  };

  const fetchWatchlist = async () => {
    try {
      setWatchlistError("");
      const res = await api(`/api/watchlist/`);
      setWatchlist(res);
    } catch (err) {
      setWatchlistError(err.message || "Failed to load the watchlist.");
    }
  };

  useEffect(() => {
    fetchDiscovery();
    fetchWatchlist();
  }, []);

  const handleUpdateWatchlist = async (symbol, action, value) => {
    try {
      setUpdating(true);
      setUpdateError("");
      const payload = { symbol };
      if (action === "delete") {
        payload.delete = true;
      } else if (action === "force_include") {
        payload.force_include = value;
      } else if (action === "force_exclude") {
        payload.force_exclude = value;
      }

      await api(`/api/watchlist/`, { method: "POST", body: payload });
      await fetchWatchlist();
    } catch (err) {
      setUpdateError(err.message || "Failed to update the watchlist.");
    } finally {
      setUpdating(false);
    }
  };

  const handleAddWatchlistItem = async (e) => {
    e.preventDefault();
    if (!newItem.trim()) return;
    try {
      setUpdating(true);
      setUpdateError("");
      await api(`/api/watchlist/`, {
        method: "POST",
        body: { symbol: newItem.trim().toUpperCase(), force_include: true },
      });
      setNewItem("");
      await fetchWatchlist();
    } catch (err) {
      setUpdateError(err.message || "Failed to add the symbol.");
    } finally {
      setUpdating(false);
    }
  };

  if (loading) return <div className="route-loading">Loading discovery data…</div>;
  if (error) {
    return (
      <div className="startup-error">
        <p className="error">{error}</p>
        <button type="button" className="primary" onClick={fetchDiscovery}>Retry</button>
      </div>
    );
  }

  const legacyBasis = basis === "usd_denominated" ? "usd_real" : "nominal";
  const leaders = data?.leaders?.[basis] ?? data?.leaders?.[legacyBasis] ?? {};
  const categories = Object.keys(leaders);

  return (
    <div className="discovery-page" style={{ padding: "1.5rem 0" }}>
      <header className="discovery-header" style={{ marginBottom: "2rem", borderBottom: "1px solid var(--border)", paddingBottom: "1.5rem" }}>
        <h2 style={{ margin: 0, fontSize: "1.75rem", fontWeight: 700 }}>Market Discovery</h2>
        <p style={{ color: "var(--muted)", marginTop: "0.25rem" }}>
          Compare historical risk-adjusted results within the currently verified candidate set.
        </p>

        {/* Toggle Controls */}
        <div style={{ display: "flex", gap: "1rem", marginTop: "1.25rem", flexWrap: "wrap" }}>
          <div>
            <label style={{ fontSize: "0.85rem", color: "var(--muted)", display: "block", marginBottom: "0.25rem" }}>Basis Mode</label>
            <div className="inline" style={{ background: "var(--panel-2)", padding: "2px", borderRadius: "8px" }}>
              <button
                type="button"
                className={basis === "nominal_toman" ? "primary small" : "small"}
                style={{ borderRadius: "6px", border: "none" }}
                aria-pressed={basis === "nominal_toman"}
                onClick={() => setBasis("nominal_toman")}
              >
                Nominal Toman
              </button>
              <button
                type="button"
                className={basis === "usd_denominated" ? "primary small" : "small"}
                style={{ borderRadius: "6px", border: "none" }}
                aria-pressed={basis === "usd_denominated"}
                onClick={() => setBasis("usd_denominated")}
              >
                USD-denominated
              </button>
            </div>
          </div>

          <div>
            <label style={{ fontSize: "0.85rem", color: "var(--muted)", display: "block", marginBottom: "0.25rem" }}>Performance Metric</label>
            <div className="inline" style={{ background: "var(--panel-2)", padding: "2px", borderRadius: "8px" }}>
              <button
                type="button"
                className={metric === "sharpe" ? "primary small" : "small"}
                style={{ borderRadius: "6px", border: "none" }}
                aria-pressed={metric === "sharpe"}
                onClick={() => setMetric("sharpe")}
              >
                Sharpe Ratio
              </button>
              <button
                type="button"
                className={metric === "sortino" ? "primary small" : "small"}
                style={{ borderRadius: "6px", border: "none" }}
                aria-pressed={metric === "sortino"}
                onClick={() => setMetric("sortino")}
              >
                Sortino Ratio
              </button>
              <button
                type="button"
                className={metric === "calmar" ? "primary small" : "small"}
                style={{ borderRadius: "6px", border: "none" }}
                aria-pressed={metric === "calmar"}
                onClick={() => setMetric("calmar")}
              >
                Calmar Ratio
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Leaderboard Cards */}
      <section style={{ marginBottom: "3rem" }}>
        <h3 style={{ fontSize: "1.25rem", marginBottom: "1rem" }}>Historical class rankings</h3>
        <p className="muted small">
          Ranked only within {data?.candidates?.length ?? 0} current candidates using the selected basis and historical estimates; rankings are not forecasts.
        </p>
        {categories.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>No risk-adjusted performance data available for current universe candidates.</p>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: "1.5rem" }}>
            {categories.map((cat) => {
              const list = leaders[cat] || [];
              return (
                <div key={cat} className="card" style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "1.25rem" }}>
                  <h4 style={{ margin: "0 0 1rem 0", fontSize: "1.1rem", borderBottom: "1px solid var(--border)", paddingBottom: "0.5rem", color: "var(--accent)" }}>
                    {cat} ranking
                  </h4>
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
                    {list.slice(0, 5).map((item, index) => {
                      const score = metric === "sharpe" ? item.sharpe : metric === "sortino" ? item.sortino : item.calmar;
                      return (
                        <div key={item.symbol} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.9rem" }}>
                          <div>
                            <span style={{ fontWeight: 600, marginRight: "0.5rem", color: "var(--muted)" }}>#{index + 1}</span>
                            <span style={{ fontWeight: 600 }}>{item.symbol}</span>
                            <span style={{ display: "block", fontSize: "0.75rem", color: "var(--muted)" }}>{item.name}</span>
                          </div>
                          <div style={{ textAlign: "right" }}>
                            <span style={{ fontSize: "1rem", fontWeight: 700, color: score > 0 ? "var(--green)" : "var(--text)" }}>
                              {score.toFixed(2)}
                            </span>
                            <span style={{ display: "block", fontSize: "0.75rem", color: "var(--muted)" }}>
                              Historical annualized mean: {(item.expected_return_annual * 100).toFixed(1)}%
                            </span>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* Watchlist Controls */}
      <section style={{ marginBottom: "3rem", background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "1.5rem" }}>
        <h3 style={{ fontSize: "1.25rem", margin: "0 0 0.5rem 0" }}>Watchlist Candidates</h3>
        <p style={{ color: "var(--muted)", fontSize: "0.85rem", marginBottom: "1.25rem" }}>
          Forcibly include or exclude specific assets from optimization algorithms. Exclusions override candidate selectors.
        </p>

        {watchlistError && (
          <div className="error inline" role="alert">
            <span>{watchlistError}</span>
            <button type="button" className="link" onClick={fetchWatchlist}>Retry</button>
          </div>
        )}
        {updateError && <p className="error small" role="alert">{updateError}</p>}

        <form onSubmit={handleAddWatchlistItem} className="responsive-form-row">
          <label className="sr-only" htmlFor="watchlist-symbol">Symbol to add</label>
          <input
            id="watchlist-symbol"
            type="text"
            placeholder="Symbol (e.g. KAMA, USD)"
            value={newItem}
            disabled={updating}
            onChange={(e) => setNewItem(e.target.value)}
            style={{ padding: "0.5rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--text)", flex: 1 }}
          />
          <button type="submit" className="primary" disabled={updating || !newItem.trim()}>
            Add Override
          </button>
        </form>

        {watchlist.items.length === 0 ? (
          <p style={{ color: "var(--muted)", fontSize: "0.9rem", textAlign: "center", padding: "1rem 0" }}>No watchlist overrides configured.</p>
        ) : (
          <div className="table-scroll">
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9rem" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--muted)", textAlign: "left" }}>
                <th style={{ padding: "0.5rem 0" }}>Symbol</th>
                <th style={{ padding: "0.5rem 0" }}>Force Include</th>
                <th style={{ padding: "0.5rem 0" }}>Force Exclude</th>
                <th style={{ padding: "0.5rem 0", textAlign: "right" }}>Action</th>
              </tr>
            </thead>
            <tbody>
              {watchlist.items.map((item) => (
                <tr key={item.id} style={{ borderBottom: "1px solid var(--chart-grid)" }}>
                  <td style={{ padding: "0.75rem 0", fontWeight: 600 }}>{item.symbol}</td>
                  <td style={{ padding: "0.75rem 0" }}>
                    <label className="sr-only" htmlFor={`include-${item.id}`}>Force include {item.symbol}</label>
                    <input
                      id={`include-${item.id}`}
                      type="checkbox"
                      checked={item.force_include}
                      disabled={updating}
                      onChange={(e) => handleUpdateWatchlist(item.symbol, "force_include", e.target.checked)}
                    />
                  </td>
                  <td style={{ padding: "0.75rem 0" }}>
                    <label className="sr-only" htmlFor={`exclude-${item.id}`}>Force exclude {item.symbol}</label>
                    <input
                      id={`exclude-${item.id}`}
                      type="checkbox"
                      checked={item.force_exclude}
                      disabled={updating}
                      onChange={(e) => handleUpdateWatchlist(item.symbol, "force_exclude", e.target.checked)}
                    />
                  </td>
                  <td style={{ padding: "0.75rem 0", textAlign: "right" }}>
                    <button
                      type="button"
                      className="danger small"
                      disabled={updating}
                      onClick={() => handleUpdateWatchlist(item.symbol, "delete")}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>

      {/* Exclusion Reasons */}
      <section style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "1.5rem" }}>
        <h3 style={{ fontSize: "1.25rem", margin: "0 0 0.5rem 0" }}>Explainable Filter Exclusions</h3>
        <p style={{ color: "var(--muted)", fontSize: "0.85rem", marginBottom: "1.25rem" }}>
          These symbols were not used. Reasons come from the data-quality and universe screening payload.
        </p>

        {(!data?.excluded || data.excluded.length === 0) ? (
          <p style={{ color: "var(--muted)", fontSize: "0.9rem", textAlign: "center", padding: "1rem 0" }}>No excluded symbols cataloged.</p>
        ) : (
          <div className="table-scroll">
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--muted)", textAlign: "left" }}>
                <th style={{ padding: "0.5rem 0" }}>Symbol</th>
                <th style={{ padding: "0.5rem 0" }}>Exclusion Reason</th>
                <th style={{ padding: "0.5rem 0" }}>Technical Details</th>
              </tr>
            </thead>
            <tbody>
              {data.excluded.map((item) => (
                <tr key={item.key} style={{ borderBottom: "1px solid var(--chart-grid)" }}>
                  <td style={{ padding: "0.75rem 0", fontWeight: 600, color: "var(--red)" }}>{item.key}</td>
                  <td style={{ padding: "0.75rem 0", textTransform: "capitalize" }}>
                    {(item.reason || "not reported").replace(/_/g, " ")}
                  </td>
                  <td style={{ padding: "0.75rem 0", color: "var(--muted)" }}>{item.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>
    </div>
  );
}

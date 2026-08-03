import { useEffect, useState } from "react";
import { getWatchlist, toggleWatchlistItem, listAccounts, listAssets } from "../api.js";

export default function Watchlist({ user }) {
  const [accounts, setAccounts] = useState([]);
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [watchlist, setWatchlist] = useState(null);
  const [watchlistLoading, setWatchlistLoading] = useState(false);
  const [assets, setAssets] = useState([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    listAccounts()
      .then((accs) => {
        setAccounts(accs);
        if (accs.length > 0) {
          setSelectedAccountId(accs[0].id);
        }
      })
      .catch((e) => setErr(e.message || "Failed to load accounts."));

    listAssets()
      .then(setAssets)
      .catch((e) => console.error("Failed to load catalog assets", e));
  }, []);

  const fetchWatchlist = (accountId) => {
    if (!accountId) return;
    setWatchlistLoading(true);
    setErr("");
    getWatchlist(accountId)
      .then(setWatchlist)
      .catch((e) => setErr(e.message || "Failed to load watchlist."))
      .finally(() => setWatchlistLoading(false));
  };

  useEffect(() => {
    fetchWatchlist(selectedAccountId);
  }, [selectedAccountId]);

  const handleToggle = (symbol, isAdding) => {
    if (!selectedAccountId) return;
    setErr("");
    setMsg("");
    toggleWatchlistItem(selectedAccountId, symbol, isAdding ? "add" : "delete")
      .then(() => {
        setMsg(`Symbol ${symbol} ${isAdding ? "added to" : "removed from"} watchlist.`);
        fetchWatchlist(selectedAccountId);
      })
      .catch((e) => setErr(e.message || "Failed to update watchlist."));
  };

  const filteredAssets = searchQuery.trim()
    ? assets.filter(
        (asset) =>
          asset.key.toLowerCase().includes(searchQuery.toLowerCase()) ||
          asset.name.toLowerCase().includes(searchQuery.toLowerCase())
      ).slice(0, 10)
    : [];

  return (
    <div className="watchlist-page dashboard" style={{ padding: "2rem" }}>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "10px" }}>
          <div>
            <h2>🛡️ Asset Watchlist Manager</h2>
            <p className="muted small">Monitor specific assets and override market data catalog rules</p>
          </div>
          {accounts.length > 0 && (
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <label htmlFor="watchlist-account-select" className="small">Active Portfolio:</label>
              <select
                id="watchlist-account-select"
                className="portfolio-select"
                value={selectedAccountId}
                onChange={(e) => setSelectedAccountId(e.target.value)}
              >
                {accounts.map((acc) => (
                  <option key={acc.id} value={acc.id}>
                    {acc.name} ({acc.broker || "No Broker"})
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>

        {err && <div className="error-banner margin-top" style={{ padding: "8px 12px", background: "var(--panel-2)", borderLeft: "4px solid var(--neg)", borderRadius: "4px" }}>{err}</div>}
        {msg && <div className="pos margin-top small" style={{ padding: "8px 12px", background: "var(--panel-2)", borderLeft: "4px solid var(--pos)", borderRadius: "4px" }}>{msg}</div>}

        {/* Add symbol search */}
        <div className="margin-top" style={{ borderTop: "1px solid var(--border)", paddingTop: "1.5rem" }}>
          <h3>Add Symbol to Watchlist</h3>
          <div style={{ display: "flex", gap: "10px", marginTop: "8px", position: "relative" }}>
            <input
              type="text"
              placeholder="Search symbol code or asset name (e.g. BTC, USD, FOMILI)..."
              className="input-text flex-grow"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              style={{ padding: "8px 12px", borderRadius: "6px", border: "1px solid var(--border)" }}
            />
          </div>

          {filteredAssets.length > 0 && (
            <div style={{
              background: "var(--panel-3)",
              border: "1px solid var(--border)",
              borderRadius: "6px",
              marginTop: "4px",
              maxHeight: "200px",
              overflowY: "auto",
              boxShadow: "0 4px 12px rgba(0,0,0,0.15)"
            }}>
              {filteredAssets.map((asset) => {
                const alreadyWatched = watchlist?.items?.some((i) => i.symbol === asset.key);
                return (
                  <div key={asset.key} style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    padding: "8px 12px",
                    borderBottom: "1px solid var(--border)"
                  }}>
                    <div>
                      <strong>{asset.key}</strong> <span className="muted small">({asset.name})</span>
                    </div>
                    <button
                      type="button"
                      className={alreadyWatched ? "btn-secondary small" : "btn-accent small"}
                      onClick={() => {
                        handleToggle(asset.key, !alreadyWatched);
                        setSearchQuery("");
                      }}
                    >
                      {alreadyWatched ? "Remove" : "Add"}
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Current Watchlist table */}
        <div className="margin-top" style={{ borderTop: "1px solid var(--border)", paddingTop: "1.5rem" }}>
          <h3>Currently Watched Assets</h3>
          {watchlistLoading ? (
            <div className="muted margin-top">Loading watchlist items…</div>
          ) : !watchlist?.items || watchlist.items.length === 0 ? (
            <div className="muted margin-top small">No assets added to your watchlist yet. Search above to add them.</div>
          ) : (
            <div style={{ border: "1px solid var(--border)", borderRadius: "8px", overflow: "hidden", marginTop: "12px" }}>
              <table className="holdings font-small" style={{ margin: 0 }}>
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Force Include</th>
                    <th>Force Exclude</th>
                    <th>Added At</th>
                    <th style={{ textAlign: "right" }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {watchlist.items.map((item) => (
                    <tr key={item.id}>
                      <td><strong>{item.symbol}</strong></td>
                      <td>
                        <span className={item.force_include ? "badge badge-success" : "badge"}>
                          {item.force_include ? "YES" : "NO"}
                        </span>
                      </td>
                      <td>
                        <span className={item.force_exclude ? "badge badge-error" : "badge"}>
                          {item.force_exclude ? "YES" : "NO"}
                        </span>
                      </td>
                      <td className="muted small">{new Date(item.created_at).toLocaleString()}</td>
                      <td style={{ textAlign: "right" }}>
                        <button
                          type="button"
                          className="btn-secondary small"
                          onClick={() => handleToggle(item.symbol, false)}
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
        </div>
      </div>
    </div>
  );
}

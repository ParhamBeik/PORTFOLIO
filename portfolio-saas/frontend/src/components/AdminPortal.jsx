import { useEffect, useState } from "react";
import { adminStatus } from "../api.js";
import { fmtNum, fmtTomanCompact } from "../format.js";

export default function AdminPortal() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [activeTab, setActiveTab] = useState("archive");
  const [searchQuery, setSearchQuery] = useState("");
  const [endpointFilter, setEndpointFilter] = useState("ALL");
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [lastRefreshedAt, setLastRefreshedAt] = useState(null);

  const loadStatus = () => {
    setLoading(true);
    setErr("");
    adminStatus()
      .then((res) => {
        setData(res);
        setLoading(false);
        setLastRefreshedAt(new Date());
      })
      .catch((error) => {
        setErr(error.message);
        setLoading(false);
      });
  };

  useEffect(() => {
    loadStatus();
    const interval = setInterval(loadStatus, 15000); // auto-refresh synced database state every 15s
    return () => clearInterval(interval);
  }, []);

  if (loading && !data) return <div className="loading">Loading Admin System Diagnostics…</div>;
  if (err) return <div className="error card">Admin Portal Error: {err}</div>;
  if (!data) return null;

  const { users, database, archive, quota, recent_snapshots } = data;

  // Calculate remaining rate limit headroom
  const remainingQuota = quota?.limit ? Math.max(0, quota.limit - quota.used) : 0;
  const quotaPct = quota?.limit ? Math.round((quota.used / quota.limit) * 100) : 0;

  const filteredGaps = (archive?.worst_gaps || []).filter((gap) => {
    if (endpointFilter !== "ALL" && gap.endpoint !== endpointFilter) return false;
    if (statusFilter === "ERRORS" && !gap.last_error && (!gap.consecutive_failures || gap.consecutive_failures === 0)) return false;
    if (statusFilter === "PENDING" && gap.missing_rows === 0) return false;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim();
      return (
        gap.symbol.toLowerCase().includes(q) ||
        gap.endpoint.toLowerCase().includes(q) ||
        (gap.last_error && gap.last_error.toLowerCase().includes(q))
      );
    }
    return true;
  });

  return (
    <div className="dashboard admin-portal">
      {/* Top Banner / System Summary */}
      <div className="card admin-header-card">
        <div className="admin-header-title">
          <div>
            <h2>⚙️ System Diagnostics & Data Warehouse Portal</h2>
            <p className="muted small">
              Real-time database state, interval syncing, data backfill tracking, rate limits, and endpoint metrics
            </p>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <div className="live-sync-indicator" title="Synced with PostgreSQL database">
              <span className="live-dot" />
              <span className="small muted">
                {lastRefreshedAt ? `Synced ${lastRefreshedAt.toLocaleTimeString()}` : "Syncing…"}
              </span>
            </div>
            <button type="button" className="btn-secondary" onClick={loadStatus}>
              🔄 Refresh Status
            </button>
          </div>
        </div>

        <div className="metric-grid admin-quick-stats">
          <div className="metric">
            <div className="metric-val pos">{archive?.progress_pct}%</div>
            <div className="metric-label">Archive Completion</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database?.announcements || 0)}</div>
            <div className="metric-label">Codal Reports</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database?.shareholders || 0)}</div>
            <div className="metric-label">Shareholder Records</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database?.stock_transaction_ticks || 0)}</div>
            <div className="metric-label">Intraday Trade Ledgers</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(remainingQuota)}</div>
            <div className="metric-label">Quota Headroom Left</div>
          </div>
          <div className="metric">
            <div className="metric-val">{users?.total || 0}</div>
            <div className="metric-label">Registered Users</div>
          </div>
        </div>
      </div>

      {/* Admin Navigation Tabs */}
      <div className="seg admin-tabs">
        <button
          type="button"
          className={activeTab === "archive" ? "active" : ""}
          onClick={() => setActiveTab("archive")}
        >
          📦 Asset Archive & Backfill
        </button>
        <button
          type="button"
          className={activeTab === "quota" ? "active" : ""}
          onClick={() => setActiveTab("quota")}
        >
          ⚡ Rate Limits & Quotas
        </button>
        <button
          type="button"
          className={activeTab === "users" ? "active" : ""}
          onClick={() => setActiveTab("users")}
        >
          👥 User & Portfolio Health
        </button>
        <button
          type="button"
          className={activeTab === "database" ? "active" : ""}
          onClick={() => setActiveTab("database")}
        >
          🗄️ Database & Storage Stats
        </button>
      </div>

      {/* TAB 1: Asset Data Archive Progress & Gaps */}
      {activeTab === "archive" && (
        <section className="card">
          <div className="card-head">
            <h3>Asset Data Archival Progress Across Endpoints</h3>
            <span className="badge">{archive?.progress_pct}% Complete</span>
          </div>

          <div className="progress-bar-wrap">
            <div
              className="progress-bar-fill"
              style={{ width: `${archive?.progress_pct || 0}%` }}
            />
          </div>

          <div className="metric-grid margin-top">
            <div className="metric">
              <div className="metric-val">{fmtNum(archive?.total_states || 0)}</div>
              <div className="metric-label">Total Instrument Endpoints</div>
            </div>
            <div className="metric">
              <div className="metric-val pos">{fmtNum(archive?.complete_states || 0)}</div>
              <div className="metric-label">Verified Complete</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(archive?.pending_states || 0)}</div>
              <div className="metric-label">Pending Backfill</div>
            </div>
            <div className="metric">
              <div className="metric-val neg">{fmtNum(archive?.failed_states || 0)}</div>
              <div className="metric-label">Concurrently Failing</div>
            </div>
          </div>

          {/* Endpoint Category Breakdown */}
          {archive?.category_summary && (
            <div className="margin-top">
              <h4>Endpoint Family Coverage Breakdown</h4>
              <div className="metric-grid margin-top font-small" style={{ gap: "12px" }}>
                {Object.entries(archive.category_summary).map(([key, info]) => (
                  <div key={key} className="card-sub-metric" style={{ padding: "10px", borderRadius: "8px", background: "var(--panel-2)", border: "1px solid var(--border)" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "4px" }}>
                      <strong>{info.label}</strong>
                      <span className="pos">{info.progress_pct}%</span>
                    </div>
                    <div className="progress-bar-wrap" style={{ height: "6px", margin: "6px 0" }}>
                      <div className="progress-bar-fill" style={{ width: `${info.progress_pct}%` }} />
                    </div>
                    <div className="muted small">
                      {info.complete_states} / {info.total_states} states verified
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="margin-top" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "10px" }}>
            <h4 style={{ margin: 0 }}>Priority Gaps & Backfill Queue ({filteredGaps.length} items)</h4>
            <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
              <input
                aria-label="Search archive gaps"
                type="text"
                placeholder="🔍 Search symbol / endpoint…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                style={{ padding: "6px 12px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--panel-2)", color: "var(--text)" }}
              />
              <select
                aria-label="Filter archive gaps by endpoint"
                value={endpointFilter}
                onChange={(e) => setEndpointFilter(e.target.value)}
                style={{ padding: "6px 12px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--panel-2)", color: "var(--text)" }}
              >
                <option value="ALL">All Endpoints (8 Families)</option>
                <option value="stock_history_unadjusted">Stock History (Unadjusted)</option>
                <option value="stock_history_adjusted">Stock History (Adjusted)</option>
                <option value="stock_candle_unadjusted">Candles (Unadjusted)</option>
                <option value="stock_candle_adjusted">Candles (Adjusted)</option>
                <option value="gold_daily">Gold & Currency Daily</option>
                <option value="codal_announcements">Codal Financial Disclosures</option>
                <option value="shareholder_records">Shareholder Roster Data</option>
                <option value="stock_transaction_ticks">Intraday Trade Ledgers</option>
              </select>
              <select
                aria-label="Filter archive gaps by status"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                style={{ padding: "6px 12px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--panel-2)", color: "var(--text)" }}
              >
                <option value="ALL">All Statuses</option>
                <option value="PENDING">Pending Missing Rows</option>
                <option value="ERRORS">Errors Only</option>
              </select>
            </div>
          </div>

          {filteredGaps.length === 0 ? (
            <p className="muted small margin-top">No endpoints match the selected filter criteria.</p>
          ) : (
            <div style={{ maxHeight: "420px", overflowY: "auto", marginTop: "12px", border: "1px solid var(--border)", borderRadius: "8px" }}>
              <table className="holdings font-small" style={{ margin: 0 }}>
                <thead style={{ position: "sticky", top: 0, background: "var(--panel-2)", zIndex: 1 }}>
                  <tr>
                    <th>Symbol</th>
                    <th>Endpoint</th>
                    <th>Stored Rows</th>
                    <th>Expected Rows</th>
                    <th>Missing Rows</th>
                    <th>Status / Last Error</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredGaps.map((gap, idx) => (
                    <tr key={idx}>
                      <td><strong>{gap.symbol}</strong></td>
                      <td><code>{gap.endpoint}</code></td>
                      <td>{fmtNum(gap.stored_rows)}</td>
                      <td>{fmtNum(gap.expected_rows)}</td>
                      <td className={gap.missing_rows > 0 ? "neg" : "pos"}>
                        <strong>{fmtNum(gap.missing_rows)}</strong>
                      </td>
                      <td>
                        {gap.last_error ? (
                          <span className="neg small truncate" title={gap.last_error}>⚠️ {gap.last_error}</span>
                        ) : gap.missing_rows > 0 ? (
                          <span className="muted small">⏳ Queued backfill</span>
                        ) : (
                          <span className="pos small">✓ Verified complete</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {/* TAB 2: API Quota & Rate Limits */}
      {activeTab === "quota" && (
        <section className="card">
          <div className="card-head">
            <h3>TSETMC & Market Provider Quotas</h3>
            <span className={quotaPct > 85 ? "badge neg" : "badge pos"}>
              {quotaPct}% Used
            </span>
          </div>

          <div className="metric-grid">
            <div className="metric">
              <div className="metric-val">{fmtNum(quota?.used || 0)} / {fmtNum(quota?.limit || 0)}</div>
              <div className="metric-label">Daily Calls Used</div>
            </div>
            <div className="metric">
              <div className="metric-val pos">{fmtNum(remainingQuota)}</div>
              <div className="metric-label">Remaining Headroom</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(quota?.live_used || 0)}</div>
              <div className="metric-label">Live Price Calls</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(quota?.archive_used || 0)}</div>
              <div className="metric-label">Archive Sync Calls</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(quota?.window_5m_used || 0)}</div>
              <div className="metric-label">Calls (Last 5 mins)</div>
            </div>
            <div className="metric">
              <div className="metric-val">{data.latest_quota_day || "Today"}</div>
              <div className="metric-label">Active Quota Date</div>
            </div>
          </div>
        </section>
      )}

      {/* TAB 3: User & Portfolio Health */}
      {activeTab === "users" && (
        <section className="card">
          <div className="card-head">
            <h3>User & Portfolio Analytics</h3>
            <span className="badge">{users?.total || 0} Registered Users</span>
          </div>

          <div className="metric-grid">
            <div className="metric">
              <div className="metric-val">{users?.total || 0}</div>
              <div className="metric-label">Total Accounts</div>
            </div>
            <div className="metric">
              <div className="metric-val pos">{users?.pro || 0}</div>
              <div className="metric-label">Pro Subscribers</div>
            </div>
            <div className="metric">
              <div className="metric-val">{users?.staff || 0}</div>
              <div className="metric-label">Staff Admins</div>
            </div>
            <div className="metric">
              <div className="metric-val">{database?.accounts || 0}</div>
              <div className="metric-label">Active Portfolios</div>
            </div>
          </div>

          <h4 className="margin-top">Recent Registrations</h4>
          <table className="holdings font-small">
            <thead>
              <tr>
                <th>User ID</th>
                <th>Email</th>
                <th>Tier</th>
                <th>Staff</th>
                <th>Joined Date</th>
              </tr>
            </thead>
            <tbody>
              {users?.recent?.map((u) => (
                <tr key={u.id}>
                  <td>#{u.id}</td>
                  <td>{u.email}</td>
                  <td>
                    <span className={u.tier === "pro" ? "badge pro-badge" : "badge"}>
                      {u.tier.toUpperCase()}
                    </span>
                  </td>
                  <td>{u.is_staff ? "Yes" : "No"}</td>
                  <td>{new Date(u.joined).toLocaleDateString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* TAB 4: Database Infrastructure */}
      {activeTab === "database" && (
        <section className="card">
          <div className="card-head">
            <h3>Database Storage & Row Counts</h3>
          </div>

          <h4 style={{ margin: "10px 0 6px 0" }}>Market History & Trade Ledgers</h4>
          <div className="metric-grid">
            <div className="metric">
              <div className="metric-val">{fmtNum(database?.stock_history_rows || 0)}</div>
              <div className="metric-label">Stock Daily Closes</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(database?.candles || 0)}</div>
              <div className="metric-label">OHLC Candlesticks</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(database?.gold_currency_rows || 0)}</div>
              <div className="metric-label">Gold/Currency History</div>
            </div>
            <div className="metric">
              <div className="metric-val pos">{fmtNum(database?.stock_transaction_ticks || 0)}</div>
              <div className="metric-label">Intraday Trade Ledgers</div>
            </div>
          </div>

          <h4 style={{ margin: "16px 0 6px 0" }}>Financial Disclosures & Roster Data</h4>
          <div className="metric-grid">
            <div className="metric">
              <div className="metric-val pos">{fmtNum(database?.announcements || 0)}</div>
              <div className="metric-label">Codal Financial Reports</div>
            </div>
            <div className="metric">
              <div className="metric-val pos">{fmtNum(database?.shareholders || 0)}</div>
              <div className="metric-label">Shareholder Roster Rows</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(database?.market_instruments || 0)}</div>
              <div className="metric-label">Market Catalog Items</div>
            </div>
          </div>

          <h4 style={{ margin: "16px 0 6px 0" }}>User & Portfolio Infrastructure</h4>
          <div className="metric-grid">
            <div className="metric">
              <div className="metric-val">{fmtNum(database?.prices || 0)}</div>
              <div className="metric-label">Live Price Ticks</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(database?.snapshots || 0)}</div>
              <div className="metric-label">Net Worth Snapshots</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(database?.accounts || 0)}</div>
              <div className="metric-label">Portfolio Accounts</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(database?.holdings || 0)}</div>
              <div className="metric-label">Total Holding Rows</div>
            </div>
            <div className="metric">
              <div className="metric-val">{fmtNum(database?.portfolio_transactions || database?.transactions || 0)}</div>
              <div className="metric-label">User Portfolio Transactions</div>
            </div>
          </div>

          <h4 className="margin-top">Recent Net Worth Heartbeats</h4>
          <table className="holdings font-small">
            <thead>
              <tr>
                <th>Snapshot ID</th>
                <th>User ID</th>
                <th>Portfolio Account</th>
                <th>Total Value</th>
                <th>Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {recent_snapshots?.map((snap) => (
                <tr key={snap.id}>
                  <td>#{snap.id}</td>
                  <td>User #{snap.user_id}</td>
                  <td>{snap.account_id ? `Account #${snap.account_id}` : "Aggregate"}</td>
                  <td>{fmtTomanCompact(snap.total)}</td>
                  <td>{new Date(snap.timestamp).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

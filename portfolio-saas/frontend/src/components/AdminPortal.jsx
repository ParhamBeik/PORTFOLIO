import { useEffect, useRef, useState } from "react";
import { adminCleanPricesExecute, adminCleanPricesScan, adminStatus, adminStatusStreamUrl, getIntegrity } from "../api.js";
import { fmtNum } from "../format.js";

export default function AdminPortal() {
  const [streamData, setStreamData] = useState(null);
  const [connectionMode, setConnectionMode] = useState("connecting"); // "sse" | "polling" | "connecting"
  const [err, setErr] = useState("");
  const [lastStreamTime, setLastStreamTime] = useState(null);

  // Filters & Controls for Log Console & Gap Inspection
  const [logSearch, setLogSearch] = useState("");
  const [logLevelFilter, setLogLevelFilter] = useState("ALL");
  const [logCategoryFilter, setLogCategoryFilter] = useState("ALL");
  const [logServiceFilter, setLogServiceFilter] = useState("ALL");
  const [endpointFilter, setEndpointFilter] = useState("ALL");
  const [gapSearch, setGapSearch] = useState("");
  const [hideCompletedGaps, setHideCompletedGaps] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);

  // Data Repair State
  const [repairState, setRepairState] = useState(null);
  const [repairLoading, setRepairLoading] = useState(false);
  const [repairMsg, setRepairMsg] = useState("");

  // Symbol Integrity & Rejected Records State
  const [integrityData, setIntegrityData] = useState(null);
  const [integrityLoading, setIntegrityLoading] = useState(false);
  const [integrityError, setIntegrityError] = useState("");

  const fetchIntegrityData = () => {
    setIntegrityLoading(true);
    setIntegrityError("");
    getIntegrity()
      .then(setIntegrityData)
      .catch((e) => setIntegrityError(e.message || "Failed to load integrity data"))
      .finally(() => setIntegrityLoading(false));
  };

  useEffect(() => {
    fetchIntegrityData();
  }, []);

  const terminalStreamRef = useRef(null);

  // Auto-scroll log console internally without scrolling the browser window
  useEffect(() => {
    if (autoScroll && terminalStreamRef.current) {
      terminalStreamRef.current.scrollTop = terminalStreamRef.current.scrollHeight;
    }
  }, [streamData?.recent_logs, autoScroll]);

  const fetchStatusFallback = () => {
    adminStatus()
      .then((res) => {
        setStreamData(res);
        setLastStreamTime(new Date());
        setErr("");
      })
      .catch((e) => {
        setErr("Failed to load admin status: " + e.message);
      });
  };

  useEffect(() => {
    let eventSource = null;
    let fallbackInterval = null;
    let fallbackTimeout = null;
    let receivedStreamData = false;

    try {
      const streamUrl = adminStatusStreamUrl();
      eventSource = new EventSource(streamUrl);

      eventSource.onopen = () => {
        setConnectionMode("sse");
        setErr("");
        if (fallbackInterval) {
          clearInterval(fallbackInterval);
          fallbackInterval = null;
        }
      };

      eventSource.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          receivedStreamData = true;
          if (fallbackTimeout) {
            clearTimeout(fallbackTimeout);
            fallbackTimeout = null;
          }
          setStreamData(payload);
          setLastStreamTime(new Date());
          setConnectionMode("sse");
          setErr("");
        } catch (e) {
          console.error("SSE parse error", e);
        }
      };

      eventSource.onerror = () => {
        setConnectionMode("polling");
        if (!fallbackInterval) {
          fetchStatusFallback();
          fallbackInterval = setInterval(fetchStatusFallback, 3000);
        }
      };

      fallbackTimeout = setTimeout(() => {
        if (!receivedStreamData) fetchStatusFallback();
      }, 3000);
    } catch (e) {
      setConnectionMode("polling");
      fetchStatusFallback();
      fallbackInterval = setInterval(fetchStatusFallback, 3000);
    }

    return () => {
      if (eventSource) eventSource.close();
      if (fallbackInterval) clearInterval(fallbackInterval);
      if (fallbackTimeout) clearTimeout(fallbackTimeout);
    };
  }, []);


  const handleScanRepair = () => {
    setRepairLoading(true);
    setRepairMsg("");
    adminCleanPricesScan()
      .then((res) => {
        setRepairState(res);
        setRepairLoading(false);
        setRepairMsg("Database price audit scan completed.");
      })
      .catch((e) => {
        setRepairLoading(false);
        setRepairMsg("Scan failed: " + e.message);
      });
  };

  const handleExecuteRepair = () => {
    const CONFIRM_PHRASE = "DELETE MISPRICED DATA";
    const typed = window.prompt(
      `This permanently erases corrupt price rows (>10% spikes) and rebuilds snapshots. This cannot be undone.\n\nType "${CONFIRM_PHRASE}" to proceed:`
    );
    if (typed !== CONFIRM_PHRASE) return;
    setRepairLoading(true);
    setRepairMsg("");
    adminCleanPricesExecute(typed)
      .then((res) => {
        setRepairState(res);
        setRepairLoading(false);
        setRepairMsg("Database cleanup executed! Corrupt price rows deleted and user snapshots rebuilt.");
        fetchStatusFallback();
      })
      .catch((e) => {
        setRepairLoading(false);
        setRepairMsg("Execution failed: " + e.message);
      });
  };

  const database = streamData?.database || {};
  const archive = streamData?.archive || {};
  const quota = streamData?.quota || {};
  const recentLogs = streamData?.recent_logs || [];
  const categorySummary = archive?.category_summary || {};
  const worstGaps = archive?.worst_gaps || [];

  // Quota metrics calculation
  const dailyUsed = quota?.daily_used || quota?.used || 0;
  const dailyLimit = quota?.limit || 10000;
  const remainingQuota = Math.max(0, dailyLimit - dailyUsed);
  const quotaPct = Math.round((dailyUsed / dailyLimit) * 100);

  const windowUsed = quota?.window_used || 0;
  const windowLimit = quota?.window_limit || 1000;
  const windowPct = Math.round((windowUsed / windowLimit) * 100);

  const formatLogTime = (ts) => {
    if (!ts) return "";
    try {
      if (ts.includes("T") || ts.includes("-")) {
        const d = new Date(ts);
        if (!isNaN(d.getTime())) {
          return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
        }
      }
    } catch (e) {}
    return ts;
  };

  // Filter logs for interactive console
  const filteredLogs = recentLogs.filter((log) => {
    // 1. Search text filter
    if (logSearch.trim()) {
      const q = logSearch.toLowerCase().trim();
      const match = (
        log.message.toLowerCase().includes(q) ||
        (log.category && log.category.toLowerCase().includes(q)) ||
        (log.logger && log.logger.toLowerCase().includes(q)) ||
        log.level.toLowerCase().includes(q)
      );
      if (!match) return false;
    }
    // 2. Log level filter
    if (logLevelFilter !== "ALL") {
      const normalizedLevel = log.level === "WARN" ? "WARNING" : log.level;
      if (normalizedLevel !== logLevelFilter) return false;
    }
    // 3. Category filter
    if (logCategoryFilter !== "ALL" && log.category !== logCategoryFilter) {
      return false;
    }
    // 4. Service container filter
    if (logServiceFilter !== "ALL") {
      const srv = log.service || "backend";
      if (srv !== logServiceFilter) return false;
    }
    return true;
  });

  // Filter missing endpoint gaps
  const filteredGaps = worstGaps.filter((gap) => {
    // 1. Endpoint family filter
    if (endpointFilter !== "ALL" && gap.endpoint !== endpointFilter) return false;
    // 2. Hide completed filter
    if (hideCompletedGaps && gap.missing_rows === 0 && gap.consecutive_failures === 0) return false;
    // 3. Gap search filter (searches by symbol/endpoint specifically)
    if (gapSearch.trim()) {
      const q = gapSearch.toLowerCase().trim();
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
      {/* Top Header Card */}
      <div className="card admin-header-card">
        <div className="admin-header-title">
          <div>
            <h2>⚙️ Real-Time System Operations & Log Console</h2>
            <p className="muted small">
              Live database state, continuous synchronization, rate limit quotas, 13-endpoint data completeness, and diagnostic log stream
            </p>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "14px" }}>
            <div className={`live-sync-indicator ${connectionMode === "sse" ? "connected" : "polling"}`}>
              <span className="live-dot" />
              <span className="small font-mono">
                {connectionMode === "sse"
                  ? `LIVE SSE (${lastStreamTime ? lastStreamTime.toLocaleTimeString() : ""})`
                  : connectionMode === "polling"
                  ? `SYNCED HTTP (3s) (${lastStreamTime ? lastStreamTime.toLocaleTimeString() : ""})`
                  : "CONNECTING…"}
              </span>
            </div>
            <button type="button" className="btn-secondary" onClick={fetchStatusFallback}>
              🔄 Refresh
            </button>
          </div>
        </div>

        {err && <div className="error-banner" style={{ marginTop: "12px", padding: "8px 12px", background: "var(--panel-2)", borderLeft: "4px solid var(--neg)", borderRadius: "4px" }}>{err}</div>}
        {!streamData && !err && (
          <div className="muted small margin-top" role="status">
            Loading live diagnostics…
          </div>
        )}

        {/* Top Important Data Metric Gauges */}
        <div className="metric-grid admin-quick-stats margin-top">
          <div className="metric card-stat">
            <div className="metric-val pos">{archive?.progress_pct ?? 0}%</div>
            <div className="metric-label">Overall Archive Completion</div>
            <div className="progress-bar-wrap" style={{ marginTop: "8px" }}>
              <div className="progress-bar-fill" style={{ width: `${archive?.progress_pct || 0}%` }} />
            </div>
          </div>

          <div className="metric card-stat">
            <div className="metric-val">{fmtNum(remainingQuota)}</div>
            <div className="metric-label">Daily API Headroom Left ({quotaPct}% Used)</div>
            <div className="progress-bar-wrap" style={{ marginTop: "8px" }}>
              <div className="progress-bar-fill warning" style={{ width: `${quotaPct}%` }} />
            </div>
          </div>

          <div className="metric card-stat">
            <div className="metric-val">{fmtNum(windowUsed)} / {fmtNum(windowLimit)}</div>
            <div className="metric-label">5-Min Sliding Window Req ({windowPct}%)</div>
            <div className="progress-bar-wrap" style={{ marginTop: "8px" }}>
              <div className="progress-bar-fill" style={{ width: `${windowPct}%`, background: windowPct > 80 ? "var(--neg)" : "var(--accent)" }} />
            </div>
          </div>

          <div className="metric card-stat">
            <div className="metric-val neg">{fmtNum(archive?.pending_states || 0)}</div>
            <div className="metric-label">Pending Backfill Endpoints</div>
          </div>
        </div>
      </div>

      {/* 13-Endpoint Family Progress Matrix */}
      <section className="card margin-top">
        <div className="card-head">
          <h3>📊 13 BrsApi Endpoint Families Coverage Matrix</h3>
          <span className="badge pos">{archive?.complete_states || 0} / {archive?.total_states || 0} Verified Complete</span>
        </div>

        <div className="endpoint-matrix-grid">
          {Object.entries(categorySummary).map(([key, info]) => {
            const isDone = info.progress_pct >= 100;
            const hasFailed = info.failed_states > 0;
            const cardClass = isDone ? "complete" : hasFailed ? "failed" : "pending";
            const pbClass = isDone ? "success" : hasFailed ? "error" : "active";

            return (
              <div key={key} className={`endpoint-card ${cardClass}`}>
                <div className="endpoint-card-header">
                  <span className="endpoint-title">{info.label}</span>
                  <span className={`endpoint-pct ${isDone ? "pos" : hasFailed ? "neg" : "muted"}`}>
                    {info.progress_pct}%
                  </span>
                </div>

                <div className="progress-bar-wrap" style={{ margin: "8px 0" }}>
                  <div
                    className={`progress-bar-fill ${pbClass}`}
                    style={{ width: `${info.progress_pct}%` }}
                  />
                </div>

                <div className="endpoint-card-stats">
                  <span>{info.complete_states} / {info.total_states} Verified</span>
                  <div style={{ display: "flex", gap: "4px" }}>
                    {info.pending_states > 0 && <span className="badge badge-warn">{info.pending_states} Pending</span>}
                    {hasFailed && <span className="badge badge-error">{info.failed_states} Failed</span>}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* Database Storage & Row Counts */}
      <section className="card margin-top">
        <div className="card-head">
          <h3>📦 Database Storage & Row Counts</h3>
          <span className="badge pos">Live DB Statistics</span>
        </div>
        <p className="muted small">
          Total record counts stored across all PostgreSQL warehouse tables
        </p>
        <div className="metric-grid margin-top">
          <div className="metric">
            <div className="metric-val">{fmtNum(database.stock_history_rows || 0)}</div>
            <div className="metric-label">Stock History Rows</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database.gold_currency_rows || 0)}</div>
            <div className="metric-label">Gold/Currency History</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database.candles || 0)}</div>
            <div className="metric-label">Market Candles (OHLC)</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database.prices || 0)}</div>
            <div className="metric-label">Live Price Ticks</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database.snapshots || 0)}</div>
            <div className="metric-label">Net Worth Snapshots</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database.transactions || 0)}</div>
            <div className="metric-label">Trade Ledger Rows</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database.announcements || 0)}</div>
            <div className="metric-label">Codal Notices</div>
          </div>
          <div className="metric">
            <div className="metric-val">{fmtNum(database.shareholders || 0)}</div>
            <div className="metric-label">Shareholder Records</div>
          </div>
        </div>
      </section>

      {/* Real-Time Log Console & Interactive Table */}
      <section className="card margin-top dark-console-card">
        <div className="card-head" style={{ borderBottom: "1px solid var(--border)", paddingBottom: "12px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span className="console-icon">🖥️</span>
            <div>
              <h3 style={{ margin: 0 }}>Industry-Standard Live System Log Console</h3>
              <span className="muted small font-mono">Real-time Log Stream Buffer ({recentLogs.length} events)</span>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" }}>
            <button
              type="button"
              className="switch-toggle-label"
              onClick={() => setAutoScroll(!autoScroll)}
              aria-pressed={autoScroll}
            >
              <span>Auto-Scroll Log Stream</span>
              <div className={`switch-toggle ${autoScroll ? "active" : ""}`}>
                <div className="switch-slider" />
              </div>
            </button>
          </div>
        </div>

        {/* Filter Controls Bar */}
        <div className="console-toolbar" style={{ display: "flex", gap: "10px", flexWrap: "wrap", margin: "12px 0" }}>
          <input
            aria-label="Search log console"
            type="text"
            className="console-input"
            placeholder="🔍 Filter log message / symbol…"
            value={logSearch}
            onChange={(e) => setLogSearch(e.target.value)}
          />

          <select
            aria-label="Filter log service"
            className="console-select"
            value={logServiceFilter}
            onChange={(e) => setLogServiceFilter(e.target.value)}
          >
            <option value="ALL">All Services (Any Container)</option>
            <option value="backend">🖥️ Backend API</option>
            <option value="celery_worker">⚙️ Celery Worker</option>
            <option value="celery_beat">📅 Celery Beat</option>
          </select>

          <select
            aria-label="Filter log level"
            className="console-select"
            value={logLevelFilter}
            onChange={(e) => setLogLevelFilter(e.target.value)}
          >
            <option value="ALL">All Levels (INFO, WARN, ERROR)</option>
            <option value="ERROR">ERROR Only</option>
            <option value="WARNING">WARNING Only</option>
            <option value="INFO">INFO Only</option>
          </select>

          <select
            aria-label="Filter log category"
            className="console-select"
            value={logCategoryFilter}
            onChange={(e) => setLogCategoryFilter(e.target.value)}
          >
            <option value="ALL">All Categories</option>
            <option value="SPIKE_BLOCKED">🛡️ Spike Blocked (&gt;10%)</option>
            <option value="FORWARD_FILL">➡️ Forward Fill Fallback</option>
            <option value="FETCH_ERROR">❌ Fetch Error</option>
            <option value="QUOTA">⚡ Quota Warning</option>
            <option value="INGEST">📥 Ingestion Event</option>
          </select>
        </div>

        {/* Console Log Output Terminal Window */}
        <div className="terminal-window">
          {filteredLogs.length === 0 ? (
            <div className="terminal-line muted small" style={{ padding: "16px", textAlign: "center" }}>
              No log stream events match the selected filter criteria.
            </div>
          ) : (
            <div className="terminal-stream" ref={terminalStreamRef}>
              {filteredLogs.map((log) => {
                let lvlClass = "lvl-info";
                if (log.level === "ERROR") lvlClass = "lvl-error";
                if (log.level === "WARNING" || log.level === "WARN") lvlClass = "lvl-warn";

                return (
                  <div key={log.id} className={`terminal-row ${lvlClass}`}>
                    <span className="log-time">{formatLogTime(log.timestamp)}</span>
                    <span className={`log-badge ${lvlClass}`}>{log.level}</span>
                    <span className="log-cat">[{log.category}]</span>
                    {log.service && (
                      <span className="badge font-mono" style={{ fontSize: "0.65rem", padding: "0px 4px", border: "1px solid var(--border)", background: "rgba(255,255,255,0.05)", color: "var(--muted)" }}>
                        {log.service}
                      </span>
                    )}
                    <span className="log-msg">{log.message}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </section>

      {/* Priority Missing Rows & Endpoint Gaps Queue */}
      <section className="card margin-top">
        <div className="card-head" style={{ flexWrap: "wrap", gap: "10px" }}>
          <div>
            <h3 style={{ margin: 0 }}>Priority Endpoint Backfill Queue & Missing Rows ({filteredGaps.length})</h3>
            <span className="muted small">Ordered by highest missing historical data rows</span>
          </div>

          <div style={{ display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap" }}>
            <input
              aria-label="Search gaps symbol"
              type="text"
              className="console-input"
              style={{ minWidth: "180px", height: "34px", padding: "4px 8px" }}
              placeholder="🔍 Search symbol…"
              value={gapSearch}
              onChange={(e) => setGapSearch(e.target.value)}
            />

            <label style={{ display: "inline-flex", alignItems: "center", gap: "6px", fontSize: "0.8rem", cursor: "pointer", color: "var(--text)" }}>
              <input
                type="checkbox"
                checked={hideCompletedGaps}
                onChange={(e) => setHideCompletedGaps(e.target.checked)}
              />
              Hide Completed
            </label>

            <select
              aria-label="Filter endpoint backfill gaps"
              className="console-select"
              value={endpointFilter}
              onChange={(e) => setEndpointFilter(e.target.value)}
            >
              <option value="ALL">All 13 Endpoint Families</option>
              <option value="stock_history_unadjusted">Stock History (Unadjusted)</option>
              <option value="stock_history_adjusted">Stock History (Adjusted)</option>
              <option value="stock_candle_unadjusted">Candles (Unadjusted)</option>
              <option value="stock_candle_adjusted">Candles (Adjusted)</option>
              <option value="gold_daily">Gold & Currency Daily</option>
              <option value="crypto_daily">Cryptocurrency Daily</option>
              <option value="commodity_daily">Commodities Daily</option>
              <option value="market_index_daily">TSE Market Index Daily</option>
              <option value="etf_nav_daily">ETF Funds NAV Daily</option>
              <option value="option_contract_daily">Options Contracts Daily</option>
              <option value="codal_announcements">Codal Disclosures</option>
              <option value="shareholder_records">Shareholder Rosters</option>
              <option value="stock_transaction_ticks">Intraday Trade Ledgers</option>
            </select>
          </div>
        </div>

        <div style={{ maxHeight: "380px", overflowY: "auto", marginTop: "12px", border: "1px solid var(--border)", borderRadius: "8px" }}>
          <table className="holdings font-small" style={{ margin: 0 }}>
            <thead style={{ position: "sticky", top: 0, background: "var(--panel-2)", zIndex: 1 }}>
              <tr>
                <th>Symbol</th>
                <th>Endpoint Family</th>
                <th>Stored / Expected Progress</th>
                <th>Missing Rows</th>
                <th>Status</th>
                <th>Last Error Diagnostic</th>
              </tr>
            </thead>
            <tbody>
              {filteredGaps.map((gap, idx) => {
                const isComplete = gap.missing_rows === 0 && gap.consecutive_failures === 0;
                const progressPct = gap.expected_rows > 0 ? Math.round((gap.stored_rows / gap.expected_rows) * 100) : isComplete ? 100 : 0;

                let statusBadge = <span className="badge badge-success">Complete</span>;
                let pbClass = "success";
                if (gap.consecutive_failures > 0) {
                  statusBadge = <span className="badge badge-error">Failed ({gap.consecutive_failures})</span>;
                  pbClass = "error";
                } else if (gap.missing_rows > 0) {
                  statusBadge = <span className="badge badge-warn">Pending</span>;
                  pbClass = "warning";
                }

                return (
                  <tr key={`${gap.endpoint}-${gap.symbol}-${idx}`}>
                    <td><strong>{gap.symbol}</strong></td>
                    <td><span className="badge">{gap.endpoint}</span></td>
                    <td style={{ minWidth: "120px" }}>
                      <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                        <span className="font-mono small">{fmtNum(gap.stored_rows)} / {fmtNum(gap.expected_rows)}</span>
                        <div className="progress-bar-wrap" style={{ height: "6px", margin: 0, border: "none", background: "rgba(255,255,255,0.05)" }}>
                          <div className={`progress-bar-fill ${pbClass}`} style={{ width: `${progressPct}%` }} />
                        </div>
                      </div>
                    </td>
                    <td>
                      <span className={gap.missing_rows > 0 ? "neg font-mono" : "pos font-mono"}>
                        {fmtNum(gap.missing_rows)}
                      </span>
                    </td>
                    <td>{statusBadge}</td>
                    <td className="muted small font-mono" style={{ maxWidth: "320px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={gap.last_error}>
                      {gap.last_error || "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* Data Repair & Outlier Cleanup Panel */}
      <section className="card margin-top">
        <div className="card-head">
          <h3>🧹 Database Outlier Cleanup & Price Spike Eraser</h3>
          <span className="badge badge-warn">DB Repair Admin Tools</span>
        </div>
        <p className="muted small">
          Audit the database for corrupted price rows (&gt;10% spikes) caused by API glitches, erase corrupted rows, and rebuild user net-worth snapshots using verified forward-filled prices.
        </p>

        <div style={{ display: "flex", gap: "12px", marginTop: "12px", flexWrap: "wrap" }}>
          <button type="button" className="btn-secondary" onClick={handleScanRepair} disabled={repairLoading}>
            🔍 {repairLoading ? "Scanning Database…" : "Scan Price Spikes"}
          </button>
          <button type="button" className="btn-danger" onClick={handleExecuteRepair} disabled={repairLoading}>
            🧹 {repairLoading ? "Executing Cleanup…" : "Erase Spikes & Rebuild Snapshots"}
          </button>
        </div>

        {repairMsg && <div className="margin-top muted small font-mono" style={{ padding: "8px 12px", background: "var(--panel-2)", borderRadius: "4px" }}>{repairMsg}</div>}

        {repairState && (
          <div className="margin-top font-small">
            <div className="metric-grid">
              <div className="metric">
                <div className="metric-val">{fmtNum(repairState.total_inspected || 0)}</div>
                <div className="metric-label">Total Prices Scanned</div>
              </div>
              <div className="metric">
                <div className="metric-val neg">{fmtNum(repairState.flagged_spikes || 0)}</div>
                <div className="metric-label">Corrupt Spikes Found</div>
              </div>
              <div className="metric">
                <div className="metric-val pos">{fmtNum(repairState.repaired_prices || 0)}</div>
                <div className="metric-label">Price Rows Erased</div>
              </div>
              <div className="metric">
                <div className="metric-val pos">{fmtNum(repairState.purged_snapshots || 0)}</div>
                <div className="metric-label">Snapshots Purged</div>
              </div>
            </div>
          </div>
        )}
      </section>

      {/* Symbol Integrity Gate Panel */}
      <section className="card margin-top">
        <div className="card-head" style={{ flexWrap: "wrap", gap: "10px" }}>
          <div>
            <h3 style={{ margin: 0 }}>🛡️ Symbols Warehouse Integrity Gate</h3>
            <span className="muted small">Coverage & Gap metrics computed against active trading rules</span>
          </div>
          <button type="button" className="btn-secondary small" onClick={fetchIntegrityData} disabled={integrityLoading}>
            {integrityLoading ? "Refreshing…" : "🔄 Refresh"}
          </button>
        </div>

        {integrityError && <div className="error margin-top">{integrityError}</div>}

        <div style={{ maxHeight: "300px", overflowY: "auto", marginTop: "12px", border: "1px solid var(--border)", borderRadius: "8px" }}>
          <table className="holdings font-small" style={{ margin: 0 }}>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Coverage Ratio</th>
                <th>Max Gap Days</th>
                <th>Passes Gate</th>
                <th>Failure Cause / Reason</th>
                <th>Last Evaluated</th>
              </tr>
            </thead>
            <tbody>
              {!integrityData?.integrity || integrityData.integrity.length === 0 ? (
                <tr>
                  <td colSpan="6" style={{ textAlign: "center", color: "var(--muted)", padding: "1rem" }}>
                    No symbol integrity data compiled. Run `data_integrity` command.
                  </td>
                </tr>
              ) : (
                integrityData.integrity.map((item, idx) => (
                  <tr key={`${item.symbol}-${idx}`}>
                    <td><strong>{item.symbol}</strong></td>
                    <td className="font-mono">{(item.coverage_ratio * 100).toFixed(1)}%</td>
                    <td className="font-mono">{item.max_gap_days} days</td>
                    <td>
                      <span className={item.passes_gate ? "badge badge-success" : "badge badge-error"}>
                        {item.passes_gate ? "PASSED" : "FAILED"}
                      </span>
                    </td>
                    <td className="muted" style={{ textTransform: "capitalize" }}>
                      {item.reason ? item.reason.replace(/_/g, " ") : "—"}
                    </td>
                    <td className="muted small">{item.computed_at ? new Date(item.computed_at).toLocaleString() : "—"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Rejected Records Panel */}
      <section className="card margin-top">
        <div className="card-head">
          <h3>❌ Rejected Data Ingest Records</h3>
          <span className="muted small">Failed raw quality validations, ranked by occurrences</span>
        </div>

        <div style={{ maxHeight: "300px", overflowY: "auto", marginTop: "12px", border: "1px solid var(--border)", borderRadius: "8px" }}>
          <table className="holdings font-small" style={{ margin: 0 }}>
            <thead>
              <tr>
                <th>Natural Key</th>
                <th>Endpoint</th>
                <th>Reject Reason</th>
                <th>Date</th>
                <th style={{ textAlign: "right" }}>Occurrences</th>
                <th>Last Seen</th>
              </tr>
            </thead>
            <tbody>
              {!integrityData?.rejected || integrityData.rejected.length === 0 ? (
                <tr>
                  <td colSpan="6" style={{ textAlign: "center", color: "var(--muted)", padding: "1rem" }}>
                    No rejected records stored. Data ingestion is healthy.
                  </td>
                </tr>
              ) : (
                integrityData.rejected.map((item) => (
                  <tr key={item.id}>
                    <td><strong>{item.symbol || "—"}</strong></td>
                    <td><span className="badge">{item.endpoint}</span></td>
                    <td className="neg" style={{ fontWeight: 600 }}>{item.reason}</td>
                    <td className="font-mono">{item.date || "—"}</td>
                    <td style={{ textAlign: "right", fontWeight: 700 }}>{fmtNum(item.occurrences)}</td>
                    <td className="muted small">{item.last_seen ? new Date(item.last_seen).toLocaleString() : "—"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { adminCleanPricesExecute, adminCleanPricesScan, adminStatus, adminStatusStreamUrl, getIntegrity, listAdminUsers, retryArchiveJob } from "../api.js";
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

  // Sub-navigation / Tabs state
  const [activeTab, setActiveTab] = useState("system"); // "system" | "workers" | "users" | "jobs" | "cleanup" | "integrity"

  // Users Search State
  const [userSearch, setUserSearch] = useState("");
  const [usersList, setUsersList] = useState([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [usersError, setUsersError] = useState("");

  // Jobs Actions State
  const [retryingJobId, setRetryingJobId] = useState(null);
  const [retryResult, setRetryResult] = useState("");
  const [jobsFilter, setJobsFilter] = useState("ALL"); // "ALL" | "FAILED" | "PENDING" | "COMPLETE"

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

  // Fetch users when Users tab is active
  useEffect(() => {
    if (activeTab === "users") {
      setUsersLoading(true);
      setUsersError("");
      const timer = setTimeout(() => {
        listAdminUsers(userSearch)
          .then(setUsersList)
          .catch((e) => setUsersError(e.message || "Failed to load users."))
          .finally(() => setUsersLoading(false));
      }, 300);
      return () => clearTimeout(timer);
    }
  }, [activeTab, userSearch]);

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

  const handleRetryJob = (jobId) => {
    setRetryingJobId(jobId);
    setRetryResult("");
    retryArchiveJob(jobId)
      .then((res) => {
        setRetryResult(res.detail || "Job queued for retry successfully.");
        fetchStatusFallback();
      })
      .catch((e) => {
        setRetryResult(`Error: ${e.message || "Failed to retry job."}`);
      })
      .finally(() => {
        setRetryingJobId(null);
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
    if (logLevelFilter !== "ALL") {
      const normalizedLevel = log.level === "WARN" ? "WARNING" : log.level;
      if (normalizedLevel !== logLevelFilter) return false;
    }
    if (logCategoryFilter !== "ALL" && log.category !== logCategoryFilter) {
      return false;
    }
    if (logServiceFilter !== "ALL") {
      const srv = log.service || "backend";
      if (srv !== logServiceFilter) return false;
    }
    return true;
  });

  // Filter missing endpoint gaps
  const filteredGaps = worstGaps.filter((gap) => {
    if (endpointFilter !== "ALL" && gap.endpoint !== endpointFilter) return false;
    if (hideCompletedGaps && gap.missing_rows === 0 && gap.consecutive_failures === 0) return false;
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

      {/* Sub-Navigation Tabs */}
      <div className="tab-navigation card margin-top" style={{ display: "flex", gap: "10px", padding: "10px", flexWrap: "wrap" }}>
        {[
          { id: "system", label: "📊 System Metrics" },
          { id: "workers", label: "👷 Celery Workers" },
          { id: "users", label: "👥 Users list" },
          { id: "jobs", label: "🔁 Backfill Jobs" },
          { id: "cleanup", label: "🧹 Data Cleanup" },
          { id: "integrity", label: "🛡️ Integrity Gate" }
        ].map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={`btn-tab ${activeTab === tab.id ? "active" : ""}`}
            onClick={() => setActiveTab(tab.id)}
            style={{
              padding: "8px 16px",
              background: activeTab === tab.id ? "var(--panel-3)" : "transparent",
              color: "var(--foreground)",
              border: "1px solid var(--border)",
              borderRadius: "6px",
              cursor: "pointer",
              fontWeight: activeTab === tab.id ? "600" : "400",
              transition: "all 0.2s"
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Active Tab View Rendering */}
      {activeTab === "system" && (
        <>
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
              <span className="muted small">Total record counts stored across all PostgreSQL warehouse tables</span>
            </div>
            <div className="metric-grid margin-top">
              <div className="metric">
                <div className="metric-val">{fmtNum(database.stock_history_rows || 0)}</div>
                <div className="metric-label">Stock History Closes</div>
              </div>
              <div className="metric">
                <div className="metric-val">{fmtNum(database.gold_currency_rows || 0)}</div>
                <div className="metric-label">Gold/Currency Closes</div>
              </div>
              <div className="metric">
                <div className="metric-val">{fmtNum(database.candles || 0)}</div>
                <div className="metric-label">Market Candles (TSE)</div>
              </div>
              <div className="metric">
                <div className="metric-val">{fmtNum(database.prices || 0)}</div>
                <div className="metric-label">Live Cached Prices</div>
              </div>
              <div className="metric">
                <div className="metric-val">{fmtNum(database.snapshots || 0)}</div>
                <div className="metric-label">Net Worth Snapshots</div>
              </div>
              <div className="metric">
                <div className="metric-val">{fmtNum(database.transactions || 0)}</div>
                <div className="metric-label">Trade Transactions</div>
              </div>
              <div className="metric">
                <div className="metric-val">{fmtNum(database.announcements || 0)}</div>
                <div className="metric-label">Codal Announcements</div>
              </div>
              <div className="metric">
                <div className="metric-val">{fmtNum(database.shareholders || 0)}</div>
                <div className="metric-label">Shareholder Records</div>
              </div>
            </div>
          </section>

          {/* Real-Time Log Console */}
          <section className="card margin-top">
            <div className="card-head" style={{ flexWrap: "wrap", gap: "10px" }}>
              <div>
                <h3 style={{ margin: 0 }}>📟 Live Container Diagnostic Log Stream</h3>
                <span className="muted small">Aggregated container logs matching live system ticks</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                <label className="checkbox-wrap small" style={{ cursor: "pointer" }}>
                  <input
                    type="checkbox"
                    checked={autoScroll}
                    onChange={(e) => setAutoScroll(e.target.checked)}
                  />
                  Auto-scroll
                </label>
                <button
                  type="button"
                  className="btn-secondary small"
                  onClick={() => {
                    setLogSearch("");
                    setLogLevelFilter("ALL");
                    setLogCategoryFilter("ALL");
                    setLogServiceFilter("ALL");
                  }}
                >
                  Clear Filters
                </button>
              </div>
            </div>

            {/* Log Stream Filters Toolbar */}
            <div className="log-filters-toolbar margin-top">
              <input
                type="text"
                placeholder="Filter logs by search term…"
                className="input-text flex-grow"
                value={logSearch}
                onChange={(e) => setLogSearch(e.target.value)}
              />

              <select
                className="portfolio-select"
                value={logLevelFilter}
                onChange={(e) => setLogLevelFilter(e.target.value)}
              >
                <option value="ALL">All Levels</option>
                <option value="INFO">INFO</option>
                <option value="WARNING">WARNING</option>
                <option value="ERROR">ERROR</option>
              </select>

              <select
                className="portfolio-select"
                value={logServiceFilter}
                onChange={(e) => setLogServiceFilter(e.target.value)}
              >
                <option value="ALL">All Containers</option>
                <option value="backend">backend</option>
                <option value="worker_live">worker_live</option>
                <option value="worker_archive">worker_archive</option>
                <option value="beat">beat</option>
                <option value="migrate">migrate</option>
              </select>

              <select
                className="portfolio-select"
                value={logCategoryFilter}
                onChange={(e) => setLogCategoryFilter(e.target.value)}
              >
                <option value="ALL">All Loggers</option>
                <option value="django.request">django.request</option>
                <option value="marketdata.ingest">marketdata.ingest</option>
                <option value="marketdata.archive">marketdata.archive</option>
                <option value="portfolio.valuation">portfolio.valuation</option>
                <option value="portfolio.live">portfolio.live</option>
              </select>
            </div>

            {/* Simulated Live TTY Screen */}
            <div className="terminal-wrap margin-top" ref={terminalStreamRef}>
              {filteredLogs.length === 0 ? (
                <div className="muted font-mono small" style={{ padding: "8px" }}>
                  [tty] Ready. No log events found matching the active filter criteria.
                </div>
              ) : (
                filteredLogs.map((log) => {
                  const levelClass =
                    log.level === "ERROR"
                      ? "log-err"
                      : log.level === "WARN" || log.level === "WARNING"
                      ? "log-warn"
                      : "log-info";

                  return (
                    <div key={log.id} className="terminal-line">
                      <span className="log-time">[{formatLogTime(log.timestamp)}]</span>{" "}
                      <span className={`log-service badge-service-${log.service || "backend"}`}>
                        {log.service || "backend"}
                      </span>{" "}
                      <span className={`log-level ${levelClass}`}>{log.level}</span>{" "}
                      {log.category && (
                        <span className="log-category">[{log.category}]</span>
                      )}{" "}
                      <span className="log-msg">{log.message}</span>
                    </div>
                  );
                })
              )}
            </div>
          </section>
        </>
      )}

      {activeTab === "workers" && (
        <section className="card margin-top">
          <div className="card-head">
            <h3>👷 Celery Workers Health & Task Load</h3>
          </div>
          <div style={{ marginTop: "12px" }}>
            {streamData?.workers && Object.keys(streamData.workers).length > 0 ? (
              Object.entries(streamData.workers).map(([name, wInfo]) => {
                if (wInfo.error) {
                  return (
                    <div key={name} className="error-banner" style={{ margin: "8px 0" }}>
                      Worker {name}: {wInfo.error}
                    </div>
                  );
                }
                if (wInfo.detail) {
                  return (
                    <div key={name} className="muted" style={{ margin: "8px 0" }}>
                      {wInfo.detail}
                    </div>
                  );
                }
                return (
                  <div key={name} className="card-stat" style={{ marginBottom: "12px", border: "1px solid var(--border)", padding: "12px", borderRadius: "6px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <strong>🟢 {name}</strong>
                      <span className="badge badge-success">{wInfo.status}</span>
                    </div>
                    <div className="metric-grid" style={{ marginTop: "12px" }}>
                      <div className="metric">
                        <div className="metric-val">{wInfo.active_tasks}</div>
                        <div className="metric-label">Active Tasks</div>
                      </div>
                      <div className="metric">
                        <div className="metric-val">{wInfo.reserved_tasks}</div>
                        <div className="metric-label">Reserved Tasks</div>
                      </div>
                      <div className="metric">
                        <div className="metric-val">{wInfo.stats?.pool?.max_concurrency || "—"}</div>
                        <div className="metric-label">Concurrency</div>
                      </div>
                    </div>
                  </div>
                );
              })
            ) : (
              <div className="muted font-small">No active Celery workers detected. Start your celery workers to sync market data.</div>
            )}
          </div>
        </section>
      )}

      {activeTab === "users" && (
        <section className="card margin-top">
          <div className="card-head" style={{ flexWrap: "wrap", gap: "10px" }}>
            <h3>👥 Registered Users list</h3>
            <input
              type="text"
              placeholder="Search by email…"
              className="input-text"
              value={userSearch}
              onChange={(e) => setUserSearch(e.target.value)}
              style={{ maxWidth: "300px", padding: "6px 12px", borderRadius: "4px", border: "1px solid var(--border)" }}
            />
          </div>
          
          {usersLoading && <div className="muted margin-top">Searching users…</div>}
          {usersError && <div className="error margin-top">{usersError}</div>}
          
          <div style={{ maxHeight: "400px", overflowY: "auto", marginTop: "12px", border: "1px solid var(--border)", borderRadius: "8px" }}>
            <table className="holdings font-small" style={{ margin: 0 }}>
              <thead>
                <tr>
                  <th>Email</th>
                  <th>First Name</th>
                  <th>Last Name</th>
                  <th>Staff</th>
                  <th>Pro</th>
                  <th>Joined</th>
                </tr>
              </thead>
              <tbody>
                {usersList.length === 0 ? (
                  <tr>
                    <td colSpan="6" style={{ textAlign: "center", color: "var(--muted)", padding: "1rem" }}>
                      No users found.
                    </td>
                  </tr>
                ) : (
                  usersList.map((usr) => (
                    <tr key={usr.id}>
                      <td><strong>{usr.email}</strong></td>
                      <td>{usr.first_name || "—"}</td>
                      <td>{usr.last_name || "—"}</td>
                      <td>
                        <span className={usr.is_staff ? "badge badge-warn" : "badge"}>
                          {usr.is_staff ? "STAFF" : "USER"}
                        </span>
                      </td>
                      <td>
                        <span className={usr.is_pro ? "badge badge-success" : "badge"}>
                          {usr.is_pro ? "PRO" : "FREE"}
                        </span>
                      </td>
                      <td className="muted small">{new Date(usr.date_joined).toLocaleString()}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {activeTab === "jobs" && (
        <section className="card margin-top">
          <div className="card-head" style={{ flexWrap: "wrap", gap: "10px" }}>
            <div>
              <h3 style={{ margin: 0 }}>🔄 Archive Backfill Jobs & Retry Panel</h3>
              <span className="muted small">Manage and trigger sync/backfill tasks manually</span>
            </div>
            <div style={{ display: "flex", gap: "8px" }}>
              {["ALL", "FAILED", "PENDING", "COMPLETE"].map((f) => (
                <button
                  key={f}
                  type="button"
                  className={`btn-secondary small ${jobsFilter === f ? "active" : ""}`}
                  onClick={() => setJobsFilter(f)}
                  style={{
                    background: jobsFilter === f ? "var(--panel-3)" : "transparent",
                  }}
                >
                  {f}
                </button>
              ))}
            </div>
          </div>

          {retryResult && (
            <div className="margin-top muted small font-mono" style={{ padding: "8px 12px", background: "var(--panel-2)", borderRadius: "4px" }}>
              {retryResult}
            </div>
          )}

          <div style={{ maxHeight: "500px", overflowY: "auto", marginTop: "12px", border: "1px solid var(--border)", borderRadius: "8px" }}>
            <table className="holdings font-small" style={{ margin: 0 }}>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Endpoint</th>
                  <th>Progress / Gaps</th>
                  <th>Status</th>
                  <th>Last Error</th>
                  <th style={{ textAlign: "right" }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {(() => {
                  const jobs = archive?.worst_gaps || [];
                  const filteredJobs = jobs.filter((job) => {
                    if (jobsFilter === "FAILED") return job.consecutive_failures > 0;
                    if (jobsFilter === "PENDING") return !job.verified_complete && job.consecutive_failures === 0;
                    if (jobsFilter === "COMPLETE") return job.verified_complete;
                    return true;
                  });

                  if (filteredJobs.length === 0) {
                    return (
                      <tr>
                        <td colSpan="6" style={{ textAlign: "center", color: "var(--muted)", padding: "1rem" }}>
                          No archive backfill jobs match this filter.
                        </td>
                      </tr>
                    );
                  }

                  return filteredJobs.map((job) => (
                    <tr key={job.id}>
                      <td><strong>{job.symbol}</strong></td>
                      <td><span className="badge">{job.endpoint}</span></td>
                      <td>{job.stored_rows} / {job.expected_rows} ({job.missing_rows} missing)</td>
                      <td>
                        {job.consecutive_failures > 0 ? (
                          <span className="badge badge-error">FAILED ({job.consecutive_failures})</span>
                        ) : job.verified_complete ? (
                          <span className="badge badge-success">COMPLETE</span>
                        ) : (
                          <span className="badge badge-warn">PENDING</span>
                        )}
                      </td>
                      <td className="muted" style={{ maxWidth: "200px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {job.last_error || "—"}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        <button
                          type="button"
                          className="btn-accent small"
                          disabled={retryingJobId === job.id}
                          onClick={() => handleRetryJob(job.id)}
                        >
                          {retryingJobId === job.id ? "Queuing…" : "🔁 Retry"}
                        </button>
                      </td>
                    </tr>
                  ));
                })()}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {activeTab === "cleanup" && (
        <section className="card margin-top">
          <h3>🧹 Database Outlier Cleanup & Price Spike Eraser</h3>
          <p className="muted small" style={{ margin: "4px 0 16px 0" }}>
            Audit the database for corrupted price rows (&gt;10% spikes) caused by API glitches, erase corrupted rows, and rebuild user net-worth snapshots using verified forward-filled prices.
          </p>

          <div style={{ display: "flex", gap: "10px" }}>
            <button type="button" className="btn-secondary" onClick={handleScanRepair} disabled={repairLoading}>
              🔍 {repairLoading ? "Scanning Database…" : "Scan Price Spikes"}
            </button>
            {repairState && (
              <button type="button" className="btn-accent" onClick={handleExecuteRepair} disabled={repairLoading}>
                🔥 Execute Data Cleanup
              </button>
            )}
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
      )}

      {activeTab === "integrity" && (
        <>
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
        </>
      )}
    </div>
  );
}

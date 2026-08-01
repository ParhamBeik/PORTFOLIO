import { useEffect, useState, useRef } from "react";
import { api } from "../api.js";

export default function TimeMachine() {
  const [runs, setRuns] = useState([]);
  const [selectedRun, setSelectedRun] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  
  // Form options
  const [basis, setBasis] = useState("nominal");
  const [customUniverse, setCustomUniverse] = useState(""); // comma-separated symbols
  
  // Results view states
  const [activeYearIndex, setActiveYearIndex] = useState(0);
  const [activeScenario, setActiveScenario] = useState("max_sharpe");
  
  const pollTimerRef = useRef(null);

  const fetchRuns = async () => {
    try {
      setLoading(true);
      setError("");
      const list = await api(`/api/backtest/`);
      setRuns(list);
      
      // Select the first completed or active run by default
      if (list.length > 0 && !selectedRun) {
        // Auto-select latest run
        fetchRunDetails(list[0].id);
      }
    } catch (err) {
      setError(err.message || "Failed to load backtest runs.");
    } finally {
      setLoading(false);
    }
  };

  const fetchRunDetails = async (runId) => {
    try {
      const details = await api(`/api/backtest/${runId}/`);
      setSelectedRun(details);
      
      // If run is running or queued, poll for updates
      if (details.status === "queued" || details.status === "running") {
        if (!pollTimerRef.current) {
          pollTimerRef.current = setTimeout(() => {
            pollTimerRef.current = null;
            fetchRunDetails(runId);
          }, 3000);
        }
      } else {
        if (pollTimerRef.current) {
          clearTimeout(pollTimerRef.current);
          pollTimerRef.current = null;
        }
      }
    } catch (err) {
      console.error("Failed to load run details:", err);
    }
  };

  useEffect(() => {
    fetchRuns();
    return () => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, []);

  const handleSubmitBacktest = async (e) => {
    e.preventDefault();
    try {
      setSubmitting(true);
      const payload = { basis };
      if (customUniverse.trim()) {
        payload.universe = customUniverse.split(",").map(s => s.trim().toUpperCase());
      }
      
      const newRun = await api(`/api/backtest/`, { method: "POST", body: payload });
      // reload
      const list = await api(`/api/backtest/`);
      setRuns(list);
      setSelectedRun(newRun);
      fetchRunDetails(newRun.id);
    } catch (err) {
      alert("Failed to submit backtest: " + err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleSelectRun = (runId) => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    fetchRunDetails(runId);
  };

  // Group years by cutoff_date
  const yearsList = selectedRun?.years || [];
  const uniqueCutoffs = Array.from(new Set(yearsList.map(y => y.cutoff_date))).sort();
  const currentCutoff = uniqueCutoffs[activeYearIndex];
  
  // Find year result matching cutoff & scenario
  const scenarioResult = yearsList.find(
    y => y.cutoff_date === currentCutoff && y.scenario === activeScenario
  );

  return (
    <div className="timemachine-page" style={{ padding: "1.5rem 0", display: "grid", gridTemplateColumns: "1fr 300px", gap: "2rem" }}>
      {/* Main Panel */}
      <div className="main-panel">
        <header style={{ marginBottom: "2rem", borderBottom: "1px solid var(--border)", paddingBottom: "1.5rem" }}>
          <h2 style={{ margin: 0, fontSize: "1.75rem", fontWeight: 700 }}>Time Machine</h2>
          <p style={{ color: "var(--muted)", marginTop: "0.25rem" }}>
            Analyze retrospective backtests, weights stability, and walk-forward rebalances over Jalali years.
          </p>
        </header>

        {selectedRun ? (
          <div>
            {/* Status Card for Running state */}
            {(selectedRun.status === "queued" || selectedRun.status === "running") && (
              <div className="card" style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "2rem", textAlign: "center", marginBottom: "2rem" }}>
                <h3 style={{ margin: 0 }}>Simulating Retrospective Engine…</h3>
                <p style={{ color: "var(--muted)" }}>Analyzing Jalali cutoffs, covariance matrices and transaction frictions.</p>
                <div style={{ background: "var(--panel-2)", height: "8px", borderRadius: "4px", width: "100%", maxWidth: "400px", margin: "1.5rem auto", overflow: "hidden" }}>
                  <div style={{ background: "var(--accent)", height: "100%", width: `${selectedRun.progress}%`, transition: "width 0.4s ease" }}></div>
                </div>
                <span style={{ fontSize: "0.9rem", color: "var(--accent)", fontWeight: 600 }}>{selectedRun.progress}% Complete</span>
              </div>
            )}

            {/* Run Failed */}
            {selectedRun.status === "failed" && (
              <div className="card" style={{ background: "var(--panel)", border: "1px solid var(--red)", borderRadius: "12px", padding: "1.5rem", marginBottom: "2rem", color: "var(--red)" }}>
                <h3 style={{ margin: "0 0 0.5rem 0" }}>Simulation Failed</h3>
                <p style={{ margin: 0, color: "var(--text)", fontSize: "0.9rem" }}>{selectedRun.error}</p>
              </div>
            )}

            {/* Ready Results */}
            {selectedRun.status === "ready" && (
              <div>
                {/* Year Cutoff Tabs */}
                <div style={{ marginBottom: "1.5rem" }}>
                  <label style={{ fontSize: "0.85rem", color: "var(--muted)", display: "block", marginBottom: "0.5rem" }}>Jalali Rebalance Date</label>
                  <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                    {uniqueCutoffs.map((cutoff, idx) => (
                      <button
                        key={cutoff}
                        type="button"
                        className={idx === activeYearIndex ? "primary" : ""}
                        onClick={() => setActiveYearIndex(idx)}
                      >
                        {cutoff}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Scenario Selector */}
                <div style={{ marginBottom: "2rem" }}>
                  <label style={{ fontSize: "0.85rem", color: "var(--muted)", display: "block", marginBottom: "0.5rem" }}>Optimization Objective</label>
                  <div style={{ display: "flex", gap: "0.5rem" }}>
                    {["max_sharpe", "min_volatility", "risk_parity", "hrp"].map((scen) => (
                      <button
                        key={scen}
                        type="button"
                        className={activeScenario === scen ? "primary small" : "small"}
                        onClick={() => setActiveScenario(scen)}
                        style={{ textTransform: "capitalize" }}
                      >
                        {scen.replace("_", " ")}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Main Results Grid */}
                {scenarioResult ? (
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1.5rem" }}>
                    
                    {/* Target Weights */}
                    <div className="card" style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "1.5rem" }}>
                      <h3 style={{ margin: "0 0 1rem 0", fontSize: "1.1rem", borderBottom: "1px solid var(--border)", paddingBottom: "0.5rem" }}>
                        Target Allocation
                      </h3>
                      {scenarioResult.target_weights && Object.keys(scenarioResult.target_weights).length > 0 ? (
                        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
                          {Object.entries(scenarioResult.target_weights).map(([symbol, weight]) => (
                            <div key={symbol}>
                              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.9rem", marginBottom: "0.25rem" }}>
                                <span style={{ fontWeight: 600 }}>{symbol}</span>
                                <span>{(weight * 100).toFixed(1)}%</span>
                              </div>
                              <div style={{ background: "var(--panel-2)", height: "6px", borderRadius: "3px", width: "100%", overflow: "hidden" }}>
                                <div style={{ background: "var(--accent)", height: "100%", width: `${weight * 100}%` }}></div>
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <p style={{ color: "var(--muted)" }}>No assets allocated in this scenario.</p>
                      )}
                    </div>

                    {/* Realized Metrics */}
                    <div className="card" style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "1.5rem" }}>
                      <h3 style={{ margin: "0 0 1rem 0", fontSize: "1.1rem", borderBottom: "1px solid var(--border)", paddingBottom: "0.5rem" }}>
                        Realized Annual Performance
                      </h3>
                      {scenarioResult.realized_metrics && !scenarioResult.realized_metrics.error ? (
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
                          <div style={{ background: "var(--panel-2)", padding: "0.75rem", borderRadius: "8px" }}>
                            <span style={{ fontSize: "0.75rem", color: "var(--muted)", display: "block" }}>Realized Return</span>
                            <span style={{ fontSize: "1.25rem", fontWeight: 700, color: "var(--green)" }}>
                              {(scenarioResult.realized_metrics.realized_return * 100).toFixed(1)}%
                            </span>
                          </div>
                          <div style={{ background: "var(--panel-2)", padding: "0.75rem", borderRadius: "8px" }}>
                            <span style={{ fontSize: "0.75rem", color: "var(--muted)", display: "block" }}>Volatility</span>
                            <span style={{ fontSize: "1.25rem", fontWeight: 700 }}>
                              {(scenarioResult.realized_metrics.realized_volatility * 100).toFixed(1)}%
                            </span>
                          </div>
                          <div style={{ background: "var(--panel-2)", padding: "0.75rem", borderRadius: "8px" }}>
                            <span style={{ fontSize: "0.75rem", color: "var(--muted)", display: "block" }}>Sharpe Ratio</span>
                            <span style={{ fontSize: "1.25rem", fontWeight: 700, color: "var(--accent)" }}>
                              {scenarioResult.realized_metrics.sharpe.toFixed(2)}
                            </span>
                          </div>
                          <div style={{ background: "var(--panel-2)", padding: "0.75rem", borderRadius: "8px" }}>
                            <span style={{ fontSize: "0.75rem", color: "var(--muted)", display: "block" }}>Max Drawdown</span>
                            <span style={{ fontSize: "1.25rem", fontWeight: 700, color: "var(--red)" }}>
                              {(scenarioResult.realized_metrics.max_drawdown * 100).toFixed(1)}%
                            </span>
                          </div>
                          <div style={{ background: "var(--panel-2)", padding: "0.75rem", borderRadius: "8px", gridColumn: "span 2" }}>
                            <span style={{ fontSize: "0.75rem", color: "var(--muted)", display: "block" }}>Transaction Frictions Drag</span>
                            <span style={{ fontSize: "1.1rem", fontWeight: 600 }}>
                              {(scenarioResult.realized_metrics.cost_drag * 100).toFixed(2)}%
                            </span>
                          </div>
                        </div>
                      ) : (
                        <div style={{ color: "var(--muted)", fontSize: "0.9rem" }}>
                          {scenarioResult.realized_metrics?.error || "No realized metrics available for this scenario."}
                        </div>
                      )}
                    </div>

                  </div>
                ) : (
                  <p style={{ color: "var(--muted)" }}>No results found for {activeScenario} on {currentCutoff}.</p>
                )}
              </div>
            )}
          </div>
        ) : (
          <div className="card" style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "2rem", textAlign: "center", color: "var(--muted)" }}>
            Select an existing backtest run or create a new walk-forward simulation from the sidebar.
          </div>
        )}
      </div>

      {/* Sidebar Panel */}
      <div className="sidebar-panel">
        {/* Create Backtest Run Form */}
        <section className="card" style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "1.25rem", marginBottom: "1.5rem" }}>
          <h3 style={{ margin: "0 0 1rem 0", fontSize: "1.1rem" }}>New Walk-Forward Run</h3>
          <form onSubmit={handleSubmitBacktest}>
            <div style={{ marginBottom: "1rem" }}>
              <label style={{ fontSize: "0.8rem", color: "var(--muted)", display: "block", marginBottom: "0.25rem" }}>Basis</label>
              <select
                value={basis}
                onChange={(e) => setBasis(e.target.value)}
                style={{ width: "100%", padding: "0.5rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--panel-2)", color: "var(--text)" }}
              >
                <option value="nominal">Nominal Toman</option>
                <option value="usd_real">USD Real Basis</option>
              </select>
            </div>

            <div style={{ marginBottom: "1.25rem" }}>
              <label style={{ fontSize: "0.8rem", color: "var(--muted)", display: "block", marginBottom: "0.25rem" }}>Custom Universe (Optional)</label>
              <input
                type="text"
                placeholder="e.g. KAMA, FARS, USD"
                value={customUniverse}
                onChange={(e) => setCustomUniverse(e.target.value)}
                style={{ width: "100%", padding: "0.5rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--panel-2)", color: "var(--text)" }}
              />
              <span style={{ fontSize: "0.7rem", color: "var(--muted)", display: "block", marginTop: "0.25rem" }}>
                Comma-separated symbol list. Omit to use full catalog.
              </span>
            </div>

            <button type="submit" className="primary" style={{ width: "100%" }} disabled={submitting}>
              {submitting ? "Queuing…" : "Run Time Machine"}
            </button>
          </form>
        </section>

        {/* List of Runs */}
        <section className="card" style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "1.25rem" }}>
          <h3 style={{ margin: "0 0 1rem 0", fontSize: "1.1rem" }}>Simulation History</h3>
          {runs.length === 0 ? (
            <p style={{ color: "var(--muted)", fontSize: "0.85rem", margin: 0 }}>No past backtest runs found.</p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
              {runs.map((r) => (
                <div
                  key={r.id}
                  onClick={() => handleSelectRun(r.id)}
                  style={{
                    padding: "0.75rem",
                    borderRadius: "8px",
                    background: selectedRun?.id === r.id ? "var(--panel-2)" : "transparent",
                    border: `1px solid ${selectedRun?.id === r.id ? "var(--accent)" : "var(--border)"}`,
                    cursor: "pointer",
                    transition: "all 0.2s ease"
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.85rem", marginBottom: "0.25rem" }}>
                    <span style={{ fontWeight: 600 }}>Run #{r.id}</span>
                    <span
                      style={{
                        fontSize: "0.75rem",
                        padding: "2px 6px",
                        borderRadius: "4px",
                        background: r.status === "ready" ? "var(--accent-bg)" : "var(--panel-2)",
                        color: r.status === "ready" ? "var(--accent)" : "var(--muted)"
                      }}
                    >
                      {r.status}
                    </span>
                  </div>
                  <span style={{ display: "block", fontSize: "0.75rem", color: "var(--muted)" }}>
                    Basis: {r.basis} · {new Date(r.created_at).toLocaleDateString()}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

import { useEffect, useState, useRef } from "react";
import { api } from "../api.js";
import { usePortfolio } from "./PortfolioContext.jsx";
import ProGate from "./ProGate.jsx";

const SCENARIOS = ["equal_weight", "max_sharpe", "min_volatility", "risk_parity", "hrp"];
const basisLabel = (value) => value === "usd_denominated" || value === "usd_real"
  ? "USD-denominated"
  : "Nominal Toman";

export default function TimeMachine({ user }) {
  const { activeId: accountId } = usePortfolio();
  const [runs, setRuns] = useState([]);
  const [selectedRun, setSelectedRun] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [stability, setStability] = useState(null);
  const [stabilityError, setStabilityError] = useState("");
  
  // Form options
  const [basis, setBasis] = useState("nominal_toman");
  const [universeMode, setUniverseMode] = useState("portfolio");
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
      setError("");
      const details = await api(`/api/backtest/${runId}/`);
      setSelectedRun(details);
      setActiveYearIndex(0);
      
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
        if (details.status === "ready") {
          setStability(null);
          setStabilityError("");
          api(`/api/backtests/${runId}/stability/`)
            .then(setStability)
            .catch((err) => setStabilityError(err.message || "Stability summary unavailable."));
        }
      }
    } catch (err) {
      setError(err.message || "Failed to load run details.");
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
      setError("");
      const payload = {
        account_id: Number(accountId),
        universe_mode: universeMode,
        basis,
        completed_years: 5,
        symbols: [],
      };
      if (customUniverse.trim()) {
        payload.symbols = customUniverse.split(",").map(s => s.trim().toUpperCase()).filter(Boolean);
      }
      
      const newRun = await api(`/api/backtest/`, { method: "POST", body: payload });
      // reload
      const list = await api(`/api/backtest/`);
      setRuns(list);
      setSelectedRun(newRun);
      fetchRunDetails(newRun.id);
    } catch (err) {
      setError(err.message || "Failed to submit the walk-forward run.");
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
  const availableScenarios = Array.from(new Set(
    yearsList.filter(y => y.cutoff_date === currentCutoff).map(y => y.scenario)
  ));
  
  // Find year result matching cutoff & scenario
  const scenarioResult = yearsList.find(
    y => y.cutoff_date === currentCutoff && y.scenario === activeScenario
  );

  return (
    <ProGate
      user={user}
      pitch="Time Machine walk-forward studies and cross-year persistence are a Pro feature."
    >
    <div className="timemachine-page">
      {/* Main Panel */}
      <div className="main-panel">
        <header style={{ marginBottom: "2rem", borderBottom: "1px solid var(--border)", paddingBottom: "1.5rem" }}>
          <h2 style={{ margin: 0, fontSize: "1.75rem", fontWeight: 700 }}>Time Machine</h2>
          <p style={{ color: "var(--muted)", marginTop: "0.25rem" }}>
            Compare which method would have served you best and which themes persisted. Max-Sharpe is unstable by design; gold or doing nothing may win, and that comparison is the product.
          </p>
        </header>

        {error && (
          <div className="error inline" role="alert">
            <span>{error}</span>
            <button
              type="button"
              className="link"
              onClick={() => selectedRun ? fetchRunDetails(selectedRun.id) : fetchRuns()}
            >
              Retry
            </button>
          </div>
        )}
        {loading && <p className="muted" role="status">Loading walk-forward runs…</p>}

        {selectedRun ? (
          <div>
            {/* Status Card for Running state */}
            {(selectedRun.status === "queued" || selectedRun.status === "running") && (
              <div className="card" aria-live="polite" style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "12px", padding: "2rem", textAlign: "center", marginBottom: "2rem" }}>
                <h3 style={{ margin: 0 }}>Running historical walk-forward analysis…</h3>
                <p style={{ color: "var(--muted)" }}>Applying point-in-time Jalali cutoffs, scenario assumptions, and transaction costs.</p>
                <div style={{ background: "var(--panel-2)", height: "8px", borderRadius: "4px", width: "100%", maxWidth: "400px", margin: "1.5rem auto", overflow: "hidden" }}>
                  <div role="progressbar" aria-label="Backtest progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow={selectedRun.progress} style={{ background: "var(--accent)", height: "100%", width: `${selectedRun.progress}%`, transition: "width 0.4s ease" }}></div>
                </div>
                <span style={{ fontSize: "0.9rem", color: "var(--accent)", fontWeight: 600 }}>{selectedRun.progress}% Complete</span>
              </div>
            )}

            {/* Run Failed */}
            {selectedRun.status === "failed" && (
              <div className="card" style={{ background: "var(--panel)", border: "1px solid var(--red)", borderRadius: "12px", padding: "1.5rem", marginBottom: "2rem", color: "var(--red)" }}>
                <h3 style={{ margin: "0 0 0.5rem 0" }}>Simulation Failed</h3>
                <p style={{ margin: 0, color: "var(--text)", fontSize: "0.9rem" }}>{selectedRun.error}</p>
                <button type="button" className="small" onClick={() => fetchRunDetails(selectedRun.id)}>Retry status</button>
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
                        aria-pressed={idx === activeYearIndex}
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
                    {(availableScenarios.length ? availableScenarios : SCENARIOS).map((scen) => (
                      <button
                        key={scen}
                        type="button"
                        className={activeScenario === scen ? "primary small" : "small"}
                        aria-pressed={activeScenario === scen}
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
                  <div className="timemachine-results-grid">
                    
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
                        <>
                        {scenarioResult.realized_metrics.degraded?.length > 0 && (
                          <div className="error inline" role="status">
                            Degraded result: {scenarioResult.realized_metrics.degraded.join(", ").replaceAll("_", " ")}
                          </div>
                        )}
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
                          <div style={{ background: "var(--panel-2)", padding: "0.75rem", borderRadius: "8px" }}>
                            <span style={{ fontSize: "0.75rem", color: "var(--muted)", display: "block" }}>Realized Return</span>
                            <span style={{ fontSize: "1.25rem", fontWeight: 700, color: "var(--green)" }}>
                              {(scenarioResult.realized_metrics.realized_return * 100).toFixed(1)}%
                            </span>
                          </div>
                          <div style={{ background: "var(--panel-2)", padding: "0.75rem", borderRadius: "8px" }}>
                            <span style={{ fontSize: "0.75rem", color: "var(--muted)", display: "block" }}>Sortino Ratio</span>
                            <span style={{ fontSize: "1.25rem", fontWeight: 700 }}>
                              {scenarioResult.realized_metrics.sortino.toFixed(2)}
                            </span>
                          </div>
                          <div style={{ background: "var(--panel-2)", padding: "0.75rem", borderRadius: "8px" }}>
                            <span style={{ fontSize: "0.75rem", color: "var(--muted)", display: "block" }}>Calmar Ratio</span>
                            <span style={{ fontSize: "1.25rem", fontWeight: 700 }}>
                              {scenarioResult.realized_metrics.calmar.toFixed(2)}
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
                        </>
                      ) : (
                        <div style={{ color: "var(--muted)", fontSize: "0.9rem" }}>
                          {scenarioResult.realized_metrics?.error || "No realized metrics available for this scenario."}
                        </div>
                      )}
                    </div>

                    <details className="card assumptions-panel timemachine-wide">
                      <summary>Reproducibility manifest and exclusions</summary>
                      {scenarioResult.realized_metrics?.manifest ? (
                        <>
                          <dl className="assumptions-grid">
                            <div><dt>Basis</dt><dd>{basisLabel(scenarioResult.realized_metrics.manifest.basis)}</dd></div>
                            <div><dt>Universe provenance</dt><dd>{scenarioResult.realized_metrics.manifest.universe_provenance?.replaceAll("_", " ")}</dd></div>
                            <div><dt>Training window</dt><dd>{scenarioResult.realized_metrics.manifest.training_window?.start} to {scenarioResult.realized_metrics.manifest.training_window?.end}</dd></div>
                            <div><dt>Evaluation window</dt><dd>{scenarioResult.realized_metrics.manifest.evaluation_window?.start} to {scenarioResult.realized_metrics.manifest.evaluation_window?.end}</dd></div>
                            <div><dt>Observations</dt><dd>{scenarioResult.realized_metrics.manifest.training_window?.observations ?? "Not reported"}</dd></div>
                            <div><dt>Risk-free rate</dt><dd>{((scenarioResult.realized_metrics.manifest.risk_free_rate_annual || 0) * 100).toFixed(1)}%</dd></div>
                            <div><dt>CPI index</dt><dd>{scenarioResult.realized_metrics.manifest.cpi_index ?? "Not reported"}</dd></div>
                            <div><dt>Maximum source timestamp</dt><dd>{scenarioResult.realized_metrics.manifest.maximum_source_data_timestamp || "Not reported"}</dd></div>
                          </dl>
                          {scenarioResult.excluded_symbols?.length > 0 ? (
                            <ul className="compact-list">
                              {scenarioResult.excluded_symbols.map((item, index) => (
                                <li key={`${item.key || item.symbol || "excluded"}-${index}`}>
                                  {item.key || item.symbol || "Asset"} — {(item.reason || String(item)).replaceAll("_", " ")}
                                </li>
                              ))}
                            </ul>
                          ) : <p className="muted small">No exclusions were reported for this scenario.</p>}
                          {scenarioResult.realized_metrics.manifest.limitations?.map((limitation) => (
                            <p key={limitation} className="muted small">{limitation}</p>
                          ))}
                        </>
                      ) : <p className="muted small">No reproducibility manifest is available for this result.</p>}
                    </details>

                  </div>
                ) : (
                  <p style={{ color: "var(--muted)" }}>No results found for {activeScenario} on {currentCutoff}.</p>
                )}
                {stabilityError && (
                  <div className="error inline small" role="alert">
                    <span>{stabilityError}</span>
                    <button type="button" className="link" onClick={() => fetchRunDetails(selectedRun.id)}>Retry</button>
                  </div>
                )}
                {stability && (
                  <details className="card assumptions-panel" style={{ marginTop: "1.5rem" }}>
                    <summary>Cross-year persistence</summary>
                    <h4>Asset selection frequency</h4>
                    <ul className="compact-list">
                      {Object.entries(stability.asset_selection_frequency || {}).map(([symbol, frequency]) => (
                        <li key={symbol}>{symbol}: {(frequency * 100).toFixed(0)}% of cutoffs; average weight {((stability.average_weight?.[symbol] || 0) * 100).toFixed(1)}%</li>
                      ))}
                    </ul>
                    <h4>Scenario rank stability</h4>
                    <ul className="compact-list">
                      {Object.entries(stability.scenario_rank_stability || {}).map(([name, values]) => (
                        <li key={name}>{name.replaceAll("_", " ")}: average rank {values.average_rank.toFixed(2)}, standard deviation {values.rank_stddev.toFixed(2)}</li>
                      ))}
                    </ul>
                    {Object.keys(stability.recurring_exclusion_reasons || {}).length > 0 && (
                      <p className="muted small">
                        Recurring exclusions: {Object.entries(stability.recurring_exclusion_reasons).map(([reason, count]) => `${reason.replaceAll("_", " ")} (${count})`).join(", ")}
                      </p>
                    )}
                  </details>
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
              <label htmlFor="backtest-basis" style={{ fontSize: "0.8rem", color: "var(--muted)", display: "block", marginBottom: "0.25rem" }}>Basis</label>
              <select
                id="backtest-basis"
                value={basis}
                onChange={(e) => setBasis(e.target.value)}
                style={{ width: "100%", padding: "0.5rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--panel-2)", color: "var(--text)" }}
              >
                <option value="nominal_toman">Nominal Toman</option>
                <option value="usd_denominated">USD-denominated</option>
              </select>
            </div>

            <div style={{ marginBottom: "1rem" }}>
              <label htmlFor="backtest-universe" style={{ fontSize: "0.8rem", color: "var(--muted)", display: "block", marginBottom: "0.25rem" }}>Universe</label>
              <select
                id="backtest-universe"
                value={universeMode}
                onChange={(event) => setUniverseMode(event.target.value)}
                style={{ width: "100%", padding: "0.5rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--panel-2)", color: "var(--text)" }}
              >
                <option value="portfolio">Portfolio / watchlist</option>
                <option value="verified_market">Verified market candidates</option>
              </select>
            </div>

            <div style={{ marginBottom: "1.25rem" }}>
              <label htmlFor="backtest-symbols" style={{ fontSize: "0.8rem", color: "var(--muted)", display: "block", marginBottom: "0.25rem" }}>Symbols (optional)</label>
              <input
                id="backtest-symbols"
                type="text"
                placeholder="e.g. KAMA, FARS, USD"
                value={customUniverse}
                onChange={(e) => setCustomUniverse(e.target.value)}
                style={{ width: "100%", padding: "0.5rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--panel-2)", color: "var(--text)" }}
              />
              <span style={{ fontSize: "0.7rem", color: "var(--muted)", display: "block", marginTop: "0.25rem" }}>
                Comma-separated symbols. Omit to use the selected universe mode.
              </span>
            </div>

            <button type="submit" className="primary" style={{ width: "100%" }} disabled={submitting || !accountId}>
              {submitting ? "Queuing…" : "Run Time Machine"}
            </button>
            {!accountId && <p className="muted small">Select a portfolio before starting a run.</p>}
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
                <button
                  type="button"
                  key={r.id}
                  onClick={() => handleSelectRun(r.id)}
                  aria-pressed={selectedRun?.id === r.id}
                  style={{
                    width: "100%",
                    color: "var(--text)",
                    textAlign: "left",
                    padding: "0.75rem",
                    borderRadius: "8px",
                    background: selectedRun?.id === r.id ? "var(--panel-2)" : "transparent",
                    border: `1px solid ${selectedRun?.id === r.id ? "var(--accent)" : "var(--border)"}`,
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
                    Basis: {basisLabel(r.basis)} · {new Date(r.created_at).toLocaleDateString()}
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
    </ProGate>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import {
  addHolding,
  createAccount,
  deleteAccount,
  listAssets,
  removeHolding,
  trade,
  transactions,
  deleteTransaction,
  updateAccount,
  updateHolding,
  valuation,
} from "../api.js";
import { subscribePrices } from "../sse.js";
import { fmtNum, fmtTehranTime, fmtToman } from "../format.js";
import NetWorthChart from "./NetWorthChart.jsx";
import { usePortfolio } from "./PortfolioContext.jsx";

const RECONCILE_MS = 60000;

export default function Portfolio({ user }) {
  const { accounts, activeId, setActive, reload } = usePortfolio();
  const [assets, setAssets] = useState([]);
  const [val, setVal] = useState(null);
  const [error, setError] = useState("");
  const [lastUpdate, setLastUpdate] = useState(null);
  const [chartDays, setChartDays] = useState(7);

  // New-account form
  const [accName, setAccName] = useState("");
  // New-holding draft
  const [holdingDraft, setHoldingDraft] = useState({ asset: "", quantity: "" });
  // Inline editors
  const [editAcc, setEditAcc] = useState(null);
  const [editHold, setEditHold] = useState(null);
  // Buy/sell form & ledger
  const [txns, setTxns] = useState([]);
  const [form, setForm] = useState({ assetKey: "", side: "buy", quantity: "" });
  const [busy, setBusy] = useState(false);
  const [tradeMsg, setTradeMsg] = useState("");
  const requestId = useRef(0);

  const activeAcct = activeId != null ? accounts.find((a) => a.id === activeId) : null;

  const loadVal = useCallback(async () => {
    const id = ++requestId.current;
    try {
      const [assetList, v] = await Promise.all([listAssets(), valuation(activeId)]);
      if (id !== requestId.current) return;
      setAssets(assetList);
      setVal(v);
      setLastUpdate(new Date());
      setError("");
    } catch (err) {
      if (id !== requestId.current) return;
      setError(err.message);
    }
  }, [activeId]);

  useEffect(() => {
    loadVal();
    const fullId = setInterval(loadVal, RECONCILE_MS);
    const stop = subscribePrices({
      onPrices: (prices) => {
        setVal((v) => (v ? { ...v, prices: { ...v.prices, ...prices } } : v));
        setLastUpdate(new Date());
      },
    });
    return () => {
      clearInterval(fullId);
      stop();
    };
  }, [loadVal]);

  useEffect(() => {
    let current = true;
    setTradeMsg("");
    if (activeId == null) {
      setTxns([]);
      return;
    }
    transactions(365, activeId)
      .then((rows) => {
        if (current) setTxns(rows);
      })
      .catch((err) => {
        if (current) {
          setTxns([]);
          setTradeMsg(`Could not load recent activity: ${err.message}`);
        }
      });
    return () => {
      current = false;
    };
  }, [activeId]);

  useEffect(() => {
    if (assets.length && !form.assetKey) {
      const first = assets.find((a) => !a.is_house && a.is_active);
      if (first) setForm((f) => ({ ...f, assetKey: first.key }));
    }
  }, [assets, form.assetKey]);

  const refreshAll = () => {
    loadVal();
    reload();
  };

  async function doCreateAccount(e) {
    e.preventDefault();
    if (!accName.trim()) return;
    try {
      await createAccount(accName.trim());
      setAccName("");
      setError("");
      refreshAll();
    } catch (err) {
      setError(err.message);
    }
  }

  async function doUpdateAccount(e) {
    e.preventDefault();
    try {
      await updateAccount(editAcc.id, {
        name: editAcc.name,
        broker: editAcc.broker,
        goal: editAcc.goal,
      });
      setEditAcc(null);
      setError("");
      refreshAll();
    } catch (err) {
      setError(err.message);
    }
  }

  async function doDeleteAccount(acct) {
    if (!window.confirm(`Delete portfolio "${acct.name}" and all its holdings?`)) return;
    try {
      await deleteAccount(acct.id);
      if (activeId === acct.id) setActive(null);
      setError("");
      refreshAll();
    } catch (err) {
      setError(err.message);
    }
  }

  async function doAddHolding(e) {
    e.preventDefault();
    if (!activeAcct) return;
    if (!holdingDraft.asset || !holdingDraft.quantity) return;
    try {
      await addHolding(activeAcct.id, holdingDraft.asset, holdingDraft.quantity);
      setHoldingDraft({ asset: "", quantity: "" });
      setError("");
      refreshAll();
    } catch (err) {
      setError(err.message);
    }
  }

  async function doSaveHolding() {
    try {
      await updateHolding(activeAcct.id, editHold.id, editHold.quantity);
      setEditHold(null);
      setError("");
      refreshAll();
    } catch (err) {
      setError(err.message);
    }
  }

  async function doRemoveHolding(id) {
    try {
      await removeHolding(activeAcct.id, id);
      setError("");
      refreshAll();
    } catch (err) {
      setError(err.message);
    }
  }

  async function doUndoTrade(transactionId) {
    if (!window.confirm("Undo this trade? This will reverse its effect on holdings.")) return;
    setTradeMsg("");
    try {
      await deleteTransaction(transactionId);
      setTradeMsg("Trade undone.");
      refreshAll();
      setTxns(await transactions(365, activeAcct.id));
    } catch (err) {
      setTradeMsg(err.message);
    }
  }

  async function submitTrade(e) {
    e.preventDefault();
    setTradeMsg("");
    if (!activeAcct) return;

    const rawQty = form.quantity;
    if (rawQty === "" || rawQty === undefined || rawQty === null) {
      setTradeMsg("Quantity is required.");
      return;
    }
    const qty = Number(rawQty);
    if (Number.isNaN(qty) || qty <= 0) {
      setTradeMsg("Quantity must be a positive number.");
      return;
    }

    setBusy(true);
    try {
      const res = await trade(activeAcct.id, {
        assetKey: form.assetKey,
        side: form.side,
        quantity: form.quantity,
      });
      const verb = res.side === "buy" ? "Bought" : "Sold";
      setTradeMsg(
        `${verb} ${fmtNum(res.quantity)} ${res.asset_key} — holding now ${fmtNum(res.holding_quantity)}.`
      );
      setForm((f) => ({ ...f, quantity: "" }));
      refreshAll();
      transactions(365, activeAcct.id).then(setTxns).catch(() => {});
    } catch (e2) {
      setTradeMsg(e2.message);
    } finally {
      setBusy(false);
    }
  }

  const editingThis = editAcc && activeAcct && editAcc.id === activeAcct.id;
  const tradeable = assets.filter((a) => !a.is_house && a.is_active);

  // Compute Asset Class Breakdown & Allocation Metrics
  const computeAllocation = () => {
    const rawItems =
      activeId == null
        ? val?.accounts?.flatMap((a) => a.items || []) || []
        : val?.items || [];

    const totalVal = Number(val?.total || 0);
    const breakdown = {
      Gold: { label: "Gold & Currency", val: 0, color: "var(--gold)" },
      Stock: { label: "TSE Stocks", val: 0, color: "var(--accent)" },
      "Real Estate": { label: "Real Estate", val: 0, color: "var(--amber)" },
      Cash: { label: "Cash & Liquid", val: 0, color: "var(--green)" },
      Other: { label: "Other Assets", val: 0, color: "var(--muted)" },
    };

    rawItems.forEach((item) => {
      const v = Number(item.value || 0);
      const c = item.class || "Other";
      if (breakdown[c]) {
        breakdown[c].val += v;
      } else {
        breakdown.Other.val += v;
      }
    });

    const segments = Object.entries(breakdown)
      .map(([key, info]) => ({
        key,
        label: info.label,
        val: info.val,
        pct: totalVal > 0 ? (info.val / totalVal) * 100 : 0,
        color: info.color,
      }))
      .filter((s) => s.val > 0);

    const topItem = [...rawItems].sort(
      (a, b) => Number(b.value || 0) - Number(a.value || 0)
    )[0];

    const liquidVal =
      breakdown.Gold.val + breakdown.Stock.val + breakdown.Cash.val;
    const liquidPct = totalVal > 0 ? (liquidVal / totalVal) * 100 : 100;

    return { segments, totalVal, topItem, liquidPct };
  };

  const { segments, totalVal, topItem, liquidPct } = computeAllocation();

  return (
    <div className="dashboard">
      {error && <div className="error">{error}</div>}

      {/* Hero Section */}
      <section className="hero">
        <div>
          <div className="hero-label">
            {activeAcct ? activeAcct.name : "All portfolios"} · Total Net Worth (Tomans)
          </div>
          <div className="hero-value">{fmtToman(val?.total)}</div>
          <div className="hero-sub">≈ ${fmtNum(val?.total_usd)} USD</div>
        </div>
        <div className="hero-meta">
          <span className="pulse" />
          Live valuation · updated {lastUpdate ? lastUpdate.toLocaleTimeString() : "—"}
        </div>
      </section>

      {/* Upgraded Net Worth Chart Section with Separated Controls */}
      <section className="card">
        <NetWorthChart
          days={chartDays}
          onDaysChange={setChartDays}
          account={activeId}
        />
      </section>

      {/* Insights & Asset Allocation Section below the chart */}
      {activeId === null ? (
        <section className="allocation-insights-container">
          <h2 className="subhead" style={{ fontSize: "1.2rem", marginBottom: "0.5rem" }}>
            Portfolio Allocation & Insights Breakdown
          </h2>
          <p className="muted small" style={{ marginBottom: "1rem" }}>
            Real-time insight into total asset distribution, portfolio divisions, and liquid capital.
          </p>

          <div className="allocation-insights-section">
            {/* Division of Portfolios (N portfolios making up the total chart) */}
            <div className="portfolio-card" style={{ gridColumn: "span 2" }}>
              <div className="portfolio-card-head">
                <span className="portfolio-card-title">Portfolios Breakdown ({accounts.length})</span>
                <span className="tag">Division Share</span>
              </div>
              <p className="muted small">
                Division of holdings across your active portfolios contributing to total net worth.
              </p>

              {val?.accounts?.map((a) => {
                const acct = accounts.find((x) => x.id === a.id);
                const acctVal = Number(a.total || 0);
                const sharePct = totalVal > 0 ? (acctVal / totalVal) * 100 : 0;
                return (
                  <div key={a.id} style={{ margin: "1rem 0" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <div>
                        <strong>{a.name}</strong>
                        {acct?.goal && <span className="tag">{acct.goal}</span>}
                        {a.broker && <span className="muted small"> · {a.broker}</span>}
                      </div>
                      <div style={{ textAlign: "right" }}>
                        <span className="portfolio-weight-badge">{sharePct.toFixed(1)}%</span>
                        <div style={{ fontWeight: 600, fontSize: "0.9rem", marginTop: 2 }}>
                          {fmtToman(a.total)}
                        </div>
                      </div>
                    </div>
                    <div className="progress-track">
                      <div
                        className="progress-fill"
                        style={{ width: `${Math.min(100, Math.max(2, sharePct))}%` }}
                      />
                    </div>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8rem", color: "var(--muted)" }}>
                      <span>{acct?.holdings?.length || 0} holdings</span>
                      <button className="link" onClick={() => setActive(a.id)}>
                        Open Portfolio →
                      </button>
                    </div>
                  </div>
                );
              })}

              <form className="inline" onSubmit={doCreateAccount} style={{ marginTop: "1.25rem" }}>
                <input
                  aria-label="New portfolio name"
                  value={accName}
                  onChange={(e) => setAccName(e.target.value)}
                  placeholder="New portfolio name (e.g. Retirement, Trading)"
                />
                <button className="primary">Create portfolio</button>
              </form>
            </div>

            {/* Asset Class Allocation Card */}
            <div className="portfolio-card">
              <div className="portfolio-card-head">
                <span className="portfolio-card-title">Asset Class Allocation</span>
                <span className="tag">Combined</span>
              </div>

              <div className="asset-allocation-bar">
                {segments.map((s) => (
                  <div
                    key={s.key}
                    className="asset-seg-fill"
                    style={{ width: `${s.pct}%`, background: s.color }}
                    title={`${s.label}: ${s.pct.toFixed(1)}%`}
                  />
                ))}
              </div>

              <div className="asset-legend">
                {segments.map((s) => (
                  <div key={s.key} className="legend-item">
                    <span className="legend-dot" style={{ background: s.color }} />
                    <span>{s.label}: <strong>{s.pct.toFixed(1)}%</strong></span>
                  </div>
                ))}
              </div>

              <div className="insight-metric-grid">
                <div className="insight-metric-box">
                  <div className="insight-metric-label">Liquidity Ratio</div>
                  <div className="insight-metric-val">{liquidPct.toFixed(1)}%</div>
                </div>
                <div className="insight-metric-box">
                  <div className="insight-metric-label">Top Asset</div>
                  <div className="insight-metric-val" style={{ fontSize: "0.85rem", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {topItem ? topItem.asset : "None"}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>
      ) : (
        /* Single Portfolio Mode: Asset Allocation + Holdings + Buy/Sell Ledger */
        <section className="card">
          <div className="account-head">
            {editingThis ? (
              <form className="inline" onSubmit={doUpdateAccount}>
                <input
                  aria-label="Portfolio name"
                  value={editAcc.name}
                  onChange={(e) => setEditAcc({ ...editAcc, name: e.target.value })}
                />
                <input
                  aria-label="Broker"
                  placeholder="broker (optional)"
                  value={editAcc.broker}
                  onChange={(e) => setEditAcc({ ...editAcc, broker: e.target.value })}
                />
                <input
                  aria-label="Portfolio goal"
                  placeholder="goal (e.g. Retirement)"
                  value={editAcc.goal}
                  onChange={(e) => setEditAcc({ ...editAcc, goal: e.target.value })}
                />
                <button className="primary">Save</button>
                <button type="button" onClick={() => setEditAcc(null)}>
                  Cancel
                </button>
              </form>
            ) : (
              <>
                <strong>{activeAcct.name}</strong>
                {activeAcct.broker && <span className="muted"> · {activeAcct.broker}</span>}
                {activeAcct.goal && <span className="tag">{activeAcct.goal}</span>}
                <span className="actions">
                  <button
                    className="link"
                    title="Edit portfolio"
                    onClick={() =>
                      setEditAcc({
                        id: activeAcct.id,
                        name: activeAcct.name,
                        broker: activeAcct.broker || "",
                        goal: activeAcct.goal || "",
                      })
                    }
                  >
                    ✎
                  </button>
                  <button
                    className="link danger"
                    title="Delete portfolio"
                    onClick={() => doDeleteAccount(activeAcct)}
                  >
                    🗑
                  </button>
                </span>
              </>
            )}
          </div>

          {/* Asset Class Allocation Progress Bar for Single Portfolio */}
          <div style={{ marginBottom: "1.5rem" }}>
            <h3 className="subhead">Asset Allocation</h3>
            <div className="asset-allocation-bar">
              {segments.map((s) => (
                <div
                  key={s.key}
                  className="asset-seg-fill"
                  style={{ width: `${s.pct}%`, background: s.color }}
                  title={`${s.label}: ${s.pct.toFixed(1)}%`}
                />
              ))}
            </div>
            <div className="asset-legend">
              {segments.map((s) => (
                <div key={s.key} className="legend-item">
                  <span className="legend-dot" style={{ background: s.color }} />
                  <span>{s.label}: <strong>{s.pct.toFixed(1)}%</strong></span>
                </div>
              ))}
            </div>
          </div>

          <h3 className="subhead">Holdings</h3>
          <table className="holdings">
            <thead>
              <tr>
                <th>Asset</th>
                <th>Class</th>
                <th>Qty</th>
                <th>Unit (T)</th>
                <th>Value (T)</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {activeAcct.holdings.length === 0 && (
                <tr>
                  <td colSpan="6" className="muted">
                    No holdings yet. Add or buy assets below.
                  </td>
                </tr>
              )}
              {activeAcct.holdings.map((h) => {
                const valuationItem = val?.items?.find((item) => item.key === h.asset_key);
                const price = valuationItem?.unit_price ?? val?.prices?.[h.asset_key] ?? 0;
                const value = valuationItem?.value ?? price * Number(h.quantity || 0);
                const editing = editHold && editHold.id === h.id;
                return (
                  <tr key={h.id}>
                    <td>
                      {h.asset_name_fa ? (
                        <span title={h.asset_name}>{h.asset_name_fa}</span>
                      ) : (
                        h.asset_name
                      )}
                    </td>
                    <td>{h.asset_class}</td>
                    <td>
                      {editing ? (
                        <span className="inline">
                          <input
                            aria-label={
                              h.is_house
                                ? "House price per square meter"
                                : `Quantity of ${h.asset_name}`
                            }
                            type="number"
                            step="any"
                            min="0.000001"
                            value={editHold.quantity}
                            onChange={(e) =>
                              setEditHold({ ...editHold, quantity: e.target.value })
                            }
                          />
                          <button className="primary" onClick={doSaveHolding}>
                            ✓
                          </button>
                          <button type="button" onClick={() => setEditHold(null)}>
                            ✕
                          </button>
                        </span>
                      ) : (
                        fmtNum(h.quantity)
                      )}
                    </td>
                    <td>{h.is_house ? "—" : fmtNum(price)}</td>
                    <td>{fmtNum(value)}</td>
                    <td>
                      <span className="actions">
                        {h.is_house && (
                          <>
                            <button
                              className="link"
                              title="Edit quantity"
                              onClick={() => setEditHold({ id: h.id, quantity: h.quantity })}
                            >
                              ✎
                            </button>
                            <button
                              className="link danger"
                              title="Remove"
                              aria-label={`Remove ${h.asset_name}`}
                              onClick={() => doRemoveHolding(h.id)}
                            >
                              ✕
                            </button>
                          </>
                        )}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <form className="inline" onSubmit={doAddHolding} style={{ marginTop: "1rem" }}>
            <select
              aria-label="Asset to add"
              value={holdingDraft.asset}
              onChange={(e) =>
                setHoldingDraft({ ...holdingDraft, asset: e.target.value })
              }
            >
              <option value="">Add real estate asset…</option>
              {assets
                .filter((a) => a.is_house)
                .map((a) => (
                  <option key={a.id} value={a.key}>
                    {a.name}
                  </option>
                ))}
            </select>
            <input
              aria-label="House price per square meter"
              type="number"
              step="any"
              min="0.000001"
              placeholder="million Tomans per m²"
              value={holdingDraft.quantity}
              onChange={(e) =>
                setHoldingDraft({ ...holdingDraft, quantity: e.target.value })
              }
            />
            <button>Add</button>
          </form>

          <h3 className="subhead">Buy / Sell Execution</h3>
          <p className="muted small">
            Trades update this portfolio's value immediately and drop a marker on the net-worth chart.
          </p>
          <form className="trade-form inline" onSubmit={submitTrade}>
            <select
              aria-label="Trade side"
              value={form.side}
              onChange={(e) => setForm((f) => ({ ...f, side: e.target.value }))}
            >
              <option value="buy">Buy</option>
              <option value="sell">Sell</option>
            </select>
            <select
              aria-label="Trade asset"
              value={form.assetKey}
              onChange={(e) => setForm((f) => ({ ...f, assetKey: e.target.value }))}
            >
              {tradeable.map((a) => (
                <option key={a.key} value={a.key}>
                  {a.name}
                </option>
              ))}
            </select>
            <input
              aria-label="Trade quantity"
              type="number"
              step="any"
              min="0"
              placeholder="Quantity"
              value={form.quantity}
              onChange={(e) => setForm((f) => ({ ...f, quantity: e.target.value }))}
            />
            <button type="submit" disabled={busy}>
              {busy ? "…" : "Execute"}
            </button>
          </form>
          {tradeMsg && (
            <p className="muted small" role="status">
              {tradeMsg}
            </p>
          )}

          <h3 className="subhead">Recent Activity</h3>
          {txns.length === 0 ? (
            <p className="muted">No trades recorded for this portfolio yet.</p>
          ) : (
            <table className="holdings">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Side</th>
                  <th>Asset</th>
                  <th>Qty</th>
                  <th>Unit (T)</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {txns.slice(0, 20).map((t, index) => (
                  <tr key={t.id}>
                    <td>{fmtTehranTime(t.timestamp)}</td>
                    <td className={t.side === "buy" ? "pos" : "neg"}>{t.side}</td>
                    <td>{t.asset_name || t.asset_key}</td>
                    <td>{fmtNum(t.quantity)}</td>
                    <td>{fmtNum(t.price_tomans)}</td>
                    <td>
                      {txns.findIndex((row) => row.asset_key === t.asset_key) === index && (
                        <button
                          className="link danger"
                          title="Undo latest trade for this asset"
                          aria-label={`Undo ${t.side} of ${t.asset_name || t.asset_key}`}
                          onClick={() => doUndoTrade(t.id)}
                        >
                          Undo
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
    </div>
  );
}

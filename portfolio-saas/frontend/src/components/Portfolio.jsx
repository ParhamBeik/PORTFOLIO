import { useCallback, useEffect, useRef, useState } from "react";
import {
  addHolding,
  createAccount,
  deleteAccount,
  listAssets,
  removeHolding,
  trade,
  transactions,
  updateAccount,
  updateHolding,
  valuation,
} from "../api.js";
import { subscribePrices } from "../sse.js";
import { fmtNum, fmtTehranTime, fmtToman } from "../format.js";
import NetWorthChart from "./NetWorthChart.jsx";
import Analytics from "./Analytics.jsx";
import Insights from "./Insights.jsx";
import Optimization from "./Optimization.jsx";
import { usePortfolio } from "./PortfolioContext.jsx";

const RECONCILE_MS = 60000; // full refresh to pick up holding edits / recompute totals

// The single portfolio page. The top-bar selector picks the active portfolio
// (`activeId`: null = "All portfolios" aggregate, or an account id); everything
// below — hero, net-worth chart, holdings, and the Pro analytics — scopes to it.
export default function Portfolio({ user }) {
  const { accounts, activeId, setActive, reload } = usePortfolio();
  const [assets, setAssets] = useState([]);
  const [val, setVal] = useState(null);
  const [error, setError] = useState("");
  const [lastUpdate, setLastUpdate] = useState(null);
  // Net-worth chart timeframe (days). "All" maps to the backend's 365-day cap.
  const [chartDays, setChartDays] = useState(30);

  // New-account form
  const [accName, setAccName] = useState("");
  // New-holding draft for the active account: { asset, quantity }
  const [holdingDraft, setHoldingDraft] = useState({ asset: "", quantity: "" });
  // Inline editors.
  const [editAcc, setEditAcc] = useState(null); // { id, name, broker, goal }
  const [editHold, setEditHold] = useState(null); // { id, quantity }
  // Buy/sell panel + ledger (single-account mode only).
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
    // Reconcile holdings/edits and the hero total periodically; live price ticks
    // arrive over SSE and merge into val.prices between these full refreshes.
    const fullId = setInterval(loadVal, RECONCILE_MS);
    const stop = subscribePrices({
      onPrices: (prices) => {
        setVal((v) => (v ? { ...v, prices: { ...v.prices, ...prices } } : v));
        setLastUpdate(new Date());
      },
    });
    return () => { clearInterval(fullId); stop(); };
  }, [loadVal]);

  // Load this portfolio's trade ledger (and default the buy dropdown) only in
  // single-account mode. "All portfolios" has no single ledger to show.
  useEffect(() => {
    let current = true;
    setTradeMsg("");
    if (activeId == null) { setTxns([]); return; }
    transactions(365, activeId)
      .then((rows) => { if (current) setTxns(rows); })
      .catch(() => { if (current) setTxns([]); });
    return () => { current = false; };
  }, [activeId]);

  useEffect(() => {
    if (assets.length && !form.assetKey) {
      const first = assets.find((a) => !a.is_house && a.is_active);
      if (first) setForm((f) => ({ ...f, assetKey: first.key }));
    }
  }, [assets, form.assetKey]);

  // Refresh the context's account list (holdings/ids) after any write, so the
  // editor and the "All portfolios" breakdown stay in sync with the backend.
  const refreshAll = () => { loadVal(); reload(); };

  async function doCreateAccount(e) {
    e.preventDefault();
    if (!accName.trim()) return;
    await createAccount(accName.trim());
    setAccName("");
    refreshAll();
  }

  async function doUpdateAccount(e) {
    e.preventDefault();
    await updateAccount(editAcc.id, {
      name: editAcc.name,
      broker: editAcc.broker,
      goal: editAcc.goal,
    });
    setEditAcc(null);
    refreshAll();
  }

  async function doDeleteAccount(acct) {
    if (!window.confirm(`Delete portfolio "${acct.name}" and all its holdings?`)) return;
    await deleteAccount(acct.id);
    if (activeId === acct.id) setActive(null);
    refreshAll();
  }

  async function doAddHolding(e) {
    e.preventDefault();
    if (!activeAcct) return;
    if (!holdingDraft.asset || !holdingDraft.quantity) return;
    await addHolding(activeAcct.id, holdingDraft.asset, holdingDraft.quantity);
    setHoldingDraft({ asset: "", quantity: "" });
    refreshAll();
  }

  async function doSaveHolding() {
    await updateHolding(activeAcct.id, editHold.id, editHold.quantity);
    setEditHold(null);
    refreshAll();
  }

  async function submitTrade(e) {
    e.preventDefault();
    setTradeMsg("");
    if (!activeAcct) return;
    const qty = Number(form.quantity);
    if (!form.assetKey || !(qty > 0)) {
      setTradeMsg("Pick an asset and a positive quantity.");
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
      setTradeMsg(`${verb} ${fmtNum(res.quantity)} ${res.asset_key} — holding now ${fmtNum(res.holding_quantity)}.`);
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

  return (
    <div className="dashboard">
      {error && <div className="error">{error}</div>}

      <section className="hero">
        <div>
          <div className="hero-label">
            {activeAcct ? activeAcct.name : "All portfolios"} · Net worth (Tomans)
          </div>
          <div className="hero-value">{fmtToman(val?.total)}</div>
          <div className="hero-sub">≈ ${fmtNum(val?.total_usd)} USD</div>
        </div>
        <div className="hero-meta">
          <span className="pulse" />
          Live · updated {lastUpdate ? lastUpdate.toLocaleTimeString() : "—"}
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Net worth</h2>
          <div className="seg tf-seg">
            {[
              { d: 7, label: "7D" },
              { d: 30, label: "30D" },
              { d: 90, label: "90D" },
              { d: 365, label: "All" },
            ].map(({ d, label }) => (
              <button key={d} type="button"
                className={chartDays === d ? "active" : ""}
                onClick={() => setChartDays(d)}>{label}</button>
            ))}
          </div>
        </div>
        <NetWorthChart days={chartDays} account={activeId} />
      </section>

      {activeAcct ? (
        // Single-portfolio mode: full holdings editor + buy/sell + ledger.
        <section className="card">
          <div className="account-head">
            {editingThis ? (
              <form className="inline" onSubmit={doUpdateAccount}>
                <input value={editAcc.name}
                  onChange={(e) => setEditAcc({ ...editAcc, name: e.target.value })} />
                <input placeholder="broker (optional)" value={editAcc.broker}
                  onChange={(e) => setEditAcc({ ...editAcc, broker: e.target.value })} />
                <input placeholder="goal (e.g. Retirement)" value={editAcc.goal}
                  onChange={(e) => setEditAcc({ ...editAcc, goal: e.target.value })} />
                <button className="primary">Save</button>
                <button type="button" onClick={() => setEditAcc(null)}>Cancel</button>
              </form>
            ) : (
              <>
                <strong>{activeAcct.name}</strong>
                {activeAcct.broker && <span className="muted"> · {activeAcct.broker}</span>}
                {activeAcct.goal && <span className="tag">{activeAcct.goal}</span>}
                <span className="actions">
                  <button className="link" title="Edit portfolio"
                    onClick={() => setEditAcc({
                      id: activeAcct.id, name: activeAcct.name,
                      broker: activeAcct.broker || "", goal: activeAcct.goal || "",
                    })}>✎</button>
                  <button className="link danger" title="Delete portfolio"
                    onClick={() => doDeleteAccount(activeAcct)}>🗑</button>
                </span>
              </>
            )}
          </div>

          <table className="holdings">
            <thead>
              <tr><th>Asset</th><th>Class</th><th>Qty</th><th>Unit (T)</th><th>Value (T)</th><th></th></tr>
            </thead>
            <tbody>
              {activeAcct.holdings.length === 0 && (
                <tr><td colSpan="6" className="muted">No holdings yet.</td></tr>
              )}
              {activeAcct.holdings.map((h) => {
                const price = val?.prices?.[assetKeyFromName(assets, h.asset_name)] || 0;
                const value = price * Number(h.quantity || 0);
                const editing = editHold && editHold.id === h.id;
                return (
                  <tr key={h.id}>
                    <td>
                      {h.asset_name_fa ? (
                        <span title={h.asset_name}>{h.asset_name_fa}</span>
                      ) : h.asset_name}
                    </td>
                    <td>{h.asset_class}</td>
                    <td>
                      {editing ? (
                        <span className="inline">
                          <input type="number" step="any" value={editHold.quantity}
                            onChange={(e) => setEditHold({ ...editHold, quantity: e.target.value })} />
                          <button className="primary" onClick={doSaveHolding}>✓</button>
                          <button type="button" onClick={() => setEditHold(null)}>✕</button>
                        </span>
                      ) : fmtNum(h.quantity)}
                    </td>
                    <td>{fmtNum(price)}</td>
                    <td>{fmtNum(value)}</td>
                    <td>
                      <span className="actions">
                        {h.is_house && (
                          <>
                            <button className="link" title="Edit quantity"
                              onClick={() => setEditHold({ id: h.id, quantity: h.quantity })}>✎</button>
                            <button className="link danger" title="Remove"
                              onClick={() => removeHolding(activeAcct.id, h.id).then(refreshAll)}>✕</button>
                          </>
                        )}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <form className="inline" onSubmit={doAddHolding}>
            <select value={holdingDraft.asset}
              onChange={(e) => setHoldingDraft({ ...holdingDraft, asset: e.target.value })}>
              <option value="">Add asset…</option>
              {assets.filter((a) => a.is_house).map((a) => (
                <option key={a.id} value={a.key}>{a.name}</option>
              ))}
            </select>
            <input type="number" step="any" placeholder="quantity"
              value={holdingDraft.quantity}
              onChange={(e) => setHoldingDraft({ ...holdingDraft, quantity: e.target.value })} />
            <button>Add</button>
          </form>

          <h3 className="subhead">Buy / Sell</h3>
          <p className="muted small">
            Trades update this portfolio's value immediately and drop a marker on
            the net-worth chart. Selling more than you hold is rejected.
          </p>
          <form className="trade-form inline" onSubmit={submitTrade}>
            <select value={form.side}
              onChange={(e) => setForm((f) => ({ ...f, side: e.target.value }))}>
              <option value="buy">Buy</option>
              <option value="sell">Sell</option>
            </select>
            <select value={form.assetKey}
              onChange={(e) => setForm((f) => ({ ...f, assetKey: e.target.value }))}>
              {tradeable.map((a) => (
                <option key={a.key} value={a.key}>{a.name}</option>
              ))}
            </select>
            <input type="number" step="any" min="0" placeholder="Quantity"
              value={form.quantity}
              onChange={(e) => setForm((f) => ({ ...f, quantity: e.target.value }))} />
            <button type="submit" disabled={busy}>{busy ? "…" : "Execute"}</button>
          </form>
          {tradeMsg && <p className="muted small">{tradeMsg}</p>}

          <h3 className="subhead">Recent activity</h3>
          {txns.length === 0 ? (
            <p className="muted">No trades recorded for this portfolio yet.</p>
          ) : (
            <table className="holdings">
              <thead>
                <tr><th>When</th><th>Side</th><th>Asset</th><th>Qty</th><th>Unit (T)</th></tr>
              </thead>
              <tbody>
                {txns.slice(0, 20).map((t) => (
                  <tr key={t.id}>
                    <td>{fmtTehranTime(t.timestamp)}</td>
                    <td className={t.side === "buy" ? "pos" : "neg"}>{t.side}</td>
                    <td>{t.asset_name || t.asset_key}</td>
                    <td>{fmtNum(t.quantity)}</td>
                    <td>{fmtNum(t.price_tomans)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ) : (
        // "All portfolios" mode: aggregate breakdown + create form.
        <section className="card">
          <h2>Portfolios</h2>
          {accounts.length === 0 && <p className="muted">No portfolios yet. Create one below.</p>}
          {val?.accounts?.length > 0 && (
            <table className="holdings">
              <thead>
                <tr><th>Name</th><th>Goal</th><th>Broker</th><th>Holdings</th><th>Total (T)</th><th></th></tr>
              </thead>
              <tbody>
                {val.accounts.map((a) => {
                  const acct = accounts.find((x) => x.id === a.id);
                  return (
                    <tr key={a.id}>
                      <td><strong>{a.name}</strong></td>
                      <td>{acct?.goal ? <span className="tag">{acct.goal}</span> : <span className="muted">—</span>}</td>
                      <td>{a.broker || <span className="muted">—</span>}</td>
                      <td>{acct?.holdings?.length || 0}</td>
                      <td>{fmtToman(a.total)}</td>
                      <td>
                        <button className="link" onClick={() => setActive(a.id)}>Open →</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          <form className="inline" onSubmit={doCreateAccount}>
            <input value={accName} onChange={(e) => setAccName(e.target.value)}
              placeholder="New portfolio name (e.g. Retirement, Trading)" />
            <button className="primary">Create portfolio</button>
          </form>
        </section>
      )}

      {/* Pro analytics, scoped to the active portfolio via ?account=. ProGate
          renders the upsell for FREE users so the sections never dead-end. */}
      <Analytics user={user} account={activeId} />
      <Insights user={user} account={activeId} />
      <Optimization user={user} account={activeId} />
    </div>
  );
}

// The valuation payload keys holdings by asset key; the account's holding only
// carries the display name, so resolve name -> key from the catalog.
function assetKeyFromName(assets, name) {
  return assets.find((a) => a.name === name)?.key;
}

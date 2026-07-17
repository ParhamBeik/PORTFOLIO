import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import {
  addHolding,
  createAccount,
  deleteAccount,
  listAccounts,
  listAssets,
  removeHolding,
  updateAccount,
  updateHolding,
  valuation,
} from "../api.js";
import { subscribePrices } from "../sse.js";
import { fmtNum, fmtToman } from "../format.js";

const RECONCILE_MS = 60000; // full refresh to pick up holding edits / recompute totals

export default function Dashboard() {
  const [accounts, setAccounts] = useState([]);
  const [assets, setAssets] = useState([]);
  const [val, setVal] = useState(null);
  const [error, setError] = useState("");
  const [lastUpdate, setLastUpdate] = useState(null);

  // New-account form
  const [accName, setAccName] = useState("");
  // New-holding form per account: { [accountId]: { asset, quantity } }
  const [holdingDraft, setHoldingDraft] = useState({});

  // Inline editors: one account / one holding at a time.
  const [editAcc, setEditAcc] = useState(null); // { id, name, broker }
  const [editHold, setEditHold] = useState(null); // { accountId, id, quantity }

  const load = useCallback(async () => {
    try {
      const [acctList, assetList, v] = await Promise.all([
        listAccounts(),
        listAssets(),
        valuation(),
      ]);
      setAccounts(acctList);
      setAssets(assetList);
      setVal(v);
      setLastUpdate(new Date());
      setError("");
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
    // Reconcile holdings/edits and the hero total periodically; live price ticks
    // arrive over SSE and merge into val.prices between these full refreshes.
    const fullId = setInterval(load, RECONCILE_MS);
    const stop = subscribePrices({
      onPrices: (prices) => {
        setVal((v) => (v ? { ...v, prices: { ...v.prices, ...prices } } : v));
        setLastUpdate(new Date());
      },
    });
    return () => { clearInterval(fullId); stop(); };
  }, [load]);

  async function doCreateAccount(e) {
    e.preventDefault();
    if (!accName.trim()) return;
    await createAccount(accName.trim());
    setAccName("");
    load();
  }

  async function doUpdateAccount(e) {
    e.preventDefault();
    await updateAccount(editAcc.id, { name: editAcc.name, broker: editAcc.broker });
    setEditAcc(null);
    load();
  }

  async function doDeleteAccount(acct) {
    if (!window.confirm(`Delete account "${acct.name}" and all its holdings?`)) return;
    await deleteAccount(acct.id);
    load();
  }

  async function doAddHolding(accountId, e) {
    e.preventDefault();
    const draft = holdingDraft[accountId] || {};
    if (!draft.asset || !draft.quantity) return;
    await addHolding(accountId, draft.asset, draft.quantity);
    setHoldingDraft({ ...holdingDraft, [accountId]: { asset: "", quantity: "" } });
    load();
  }

  async function doSaveHolding() {
    await updateHolding(editHold.accountId, editHold.id, editHold.quantity);
    setEditHold(null);
    load();
  }

  return (
    <div className="dashboard">
      {error && <div className="error">{error}</div>}

      <section className="hero">
        <div>
          <div className="hero-label">Net worth (Tomans)</div>
          <div className="hero-value">{fmtToman(val?.total)}</div>
          <div className="hero-sub">≈ ${fmtNum(val?.total_usd)} USD</div>
        </div>
        <div className="hero-meta">
          <span className="pulse" />
          Live · updated {lastUpdate ? lastUpdate.toLocaleTimeString() : "—"}
        </div>
      </section>

      <section className="card">
        <h2>Accounts</h2>
        {accounts.length === 0 && <p className="muted">No accounts yet. Create one below.</p>}
        {accounts.map((acct) => {
          const draft = holdingDraft[acct.id] || { asset: "", quantity: "" };
          const editingThis = editAcc && editAcc.id === acct.id;
          return (
            <div key={acct.id} className="account">
              <div className="account-head">
                {editingThis ? (
                  <form className="inline" onSubmit={doUpdateAccount}>
                    <input value={editAcc.name}
                      onChange={(e) => setEditAcc({ ...editAcc, name: e.target.value })} />
                    <input placeholder="broker (optional)" value={editAcc.broker}
                      onChange={(e) => setEditAcc({ ...editAcc, broker: e.target.value })} />
                    <button className="primary">Save</button>
                    <button type="button" onClick={() => setEditAcc(null)}>Cancel</button>
                  </form>
                ) : (
                  <>
                    <Link to={`/accounts/${acct.id}`}><strong>{acct.name}</strong></Link>
                    {acct.broker && <span className="muted"> · {acct.broker}</span>}
                    <span className="actions">
                      <button className="link" title="Edit account"
                        onClick={() => setEditAcc({ id: acct.id, name: acct.name, broker: acct.broker || "" })}>✎</button>
                      <button className="link danger" title="Delete account"
                        onClick={() => doDeleteAccount(acct)}>🗑</button>
                    </span>
                  </>
                )}
              </div>
              <table className="holdings">
                <thead>
                  <tr><th>Asset</th><th>Class</th><th>Qty</th><th>Unit (T)</th><th>Value (T)</th><th></th></tr>
                </thead>
                <tbody>
                  {acct.holdings.length === 0 && (
                    <tr><td colSpan="6" className="muted">No holdings yet.</td></tr>
                  )}
                  {acct.holdings.map((h) => {
                    const price = val?.prices?.[assetKeyFromName(assets, h.asset_name)] || 0;
                    const value = price * Number(h.quantity || 0);
                    const editing = editHold && editHold.id === h.id;
                    return (
                      <tr key={h.id}>
                        <td>{h.asset_name}</td>
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
                            <button className="link" title="Edit quantity"
                              onClick={() => setEditHold({ accountId: acct.id, id: h.id, quantity: h.quantity })}>✎</button>
                            <button className="link danger" title="Remove"
                              onClick={() => removeHolding(acct.id, h.id).then(load)}>✕</button>
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              <form className="inline" onSubmit={(e) => doAddHolding(acct.id, e)}>
                <select value={draft.asset}
                  onChange={(e) => setHoldingDraft({
                    ...holdingDraft, [acct.id]: { ...draft, asset: e.target.value },
                  })}>
                  <option value="">Add asset…</option>
                  {assets.map((a) => <option key={a.id} value={a.key}>{a.name}</option>)}
                </select>
                <input type="number" step="any" placeholder="quantity"
                  value={draft.quantity}
                  onChange={(e) => setHoldingDraft({
                    ...holdingDraft, [acct.id]: { ...draft, quantity: e.target.value },
                  })} />
                <button>Add</button>
              </form>
            </div>
          );
        })}

        <form className="inline" onSubmit={doCreateAccount}>
          <input value={accName} onChange={(e) => setAccName(e.target.value)}
            placeholder="New account name (e.g. Main, Brokerage)" />
          <button className="primary">Create account</button>
        </form>
      </section>
    </div>
  );
}

// The valuation payload keys holdings by asset key; the account's holding only
// carries the display name, so resolve name -> key from the catalog.
function assetKeyFromName(assets, name) {
  return assets.find((a) => a.name === name)?.key;
}

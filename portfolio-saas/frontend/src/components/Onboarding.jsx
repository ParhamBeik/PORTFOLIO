import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { addHolding, createAccount, latestPrices, listAccounts, listAssets } from "../api.js";
import { fmtToman } from "../format.js";
import { usePortfolio } from "./PortfolioContext.jsx";
import Logo from "./Logo.jsx";

// The one gate every new user must clear before seeing the dashboard: at
// least one asset with a quantity. Creates a default account transparently
// so the flow is "pick an asset, enter how much" — not "first, set up an
// account".
export default function Onboarding() {
  const { reload } = usePortfolio();
  const navigate = useNavigate();
  const [assets, setAssets] = useState([]);
  const [prices, setPrices] = useState({});
  const [assetKey, setAssetKey] = useState("");
  const [quantity, setQuantity] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    listAssets().then((list) => {
      setAssets(list);
      const first = list.find((a) => !a.is_house && a.is_active);
      if (first) setAssetKey(first.key);
    }).catch(() => {});
    latestPrices().then(setPrices).catch(() => {});
  }, []);

  const tradeable = assets.filter((a) => !a.is_house && a.is_active);
  const qtyNum = Number(quantity);
  const unitPriceRials = Number(prices[assetKey] || 0);
  const previewRials = Number.isFinite(qtyNum) && qtyNum > 0 ? qtyNum * unitPriceRials : null;

  async function submit(e) {
    e.preventDefault();
    if (!assetKey || !qtyNum || qtyNum <= 0) {
      setError("Choose an asset and enter a quantity greater than zero.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const accounts = await createAccount("Main Portfolio").catch((err) => {
        // A default account may already exist from a previous attempt.
        if (err.status === 400) return null;
        throw err;
      });
      let accountId = accounts?.id;
      if (!accountId) {
        const list = await listAccounts();
        const main = list.find((a) => a.name === "Main Portfolio") || list[0];
        if (!main) throw new Error("Could not find or create a portfolio.");
        accountId = main.id;
      }
      await addHolding(accountId, assetKey, qtyNum);
      await reload();
      navigate("/", { replace: true });
    } catch (err) {
      setError(err.message || "Could not save your first holding.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="onboarding">
      <div className="auth-header">
        <h1 className="brand-heading"><Logo size={32} /><span>Welcome to Lattice</span></h1>
        <p className="subtitle">
          Add your first asset to unlock the dashboard. You can add more, edit,
          or import a full history any time after this.
        </p>
      </div>

      {error && <div className="error-banner" role="alert"><div className="error-text">{error}</div></div>}

      <form className="card" onSubmit={submit}>
        <div className="form-group">
          <label htmlFor="onboarding-asset">Asset</label>
          <select
            id="onboarding-asset"
            value={assetKey}
            onChange={(e) => setAssetKey(e.target.value)}
          >
            {tradeable.map((a) => (
              <option key={a.key} value={a.key}>{a.name_fa || a.name}</option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label htmlFor="onboarding-quantity">Quantity you currently hold</label>
          <input
            id="onboarding-quantity"
            type="number"
            step="any"
            min="0.000001"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            placeholder="e.g. 2.5"
          />
          {previewRials != null && (
            <p className="field-hint">≈ {fmtToman(previewRials)} at the latest price</p>
          )}
        </div>
        <button type="submit" className="primary big submit-btn" disabled={busy}>
          {busy ? "Saving…" : "Start tracking"}
        </button>
      </form>
    </div>
  );
}

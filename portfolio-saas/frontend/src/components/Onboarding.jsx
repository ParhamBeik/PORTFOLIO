// The closed-beta onboarding flow: portfolio -> baseline choice -> entry ->
// review -> confirm -> dashboard.
//
// The whole point is that nothing is written until the user confirms. An
// opening baseline states "this is what I hold today" without inventing a
// purchase price or any earlier performance; a historical ledger replays real
// events. Both land in the same immutable ledger, so the two paths differ only
// in what the user can be asked to remember.
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  accountDataQuality,
  commitImport,
  createAccount,
  createLedgerEntry,
  listAssets,
  previewImport,
} from "../api.js";
import { usePortfolio } from "./PortfolioContext.jsx";

const STEPS = [
  { id: "portfolio", label: "Portfolio" },
  { id: "baseline", label: "Baseline" },
  { id: "entry", label: "Positions" },
  { id: "review", label: "Review" },
  { id: "confirm", label: "Confirm" },
];

const CSV_HEADER =
  "external_id,occurred_at,kind,asset_key,quantity,unit_price_tomans,amount_tomans,note";

const blankRow = () => ({ assetKey: "", quantity: "" });

export default function Onboarding() {
  const navigate = useNavigate();
  const { reload, setActive } = usePortfolio();

  const [step, setStep] = useState("portfolio");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [name, setName] = useState("Main");
  const [broker, setBroker] = useState("");
  const [account, setAccount] = useState(null);

  const [mode, setMode] = useState("baseline"); // "baseline" | "history"
  const [assets, setAssets] = useState([]);
  const [assetsError, setAssetsError] = useState("");
  const [rows, setRows] = useState([blankRow()]);
  const [openingCash, setOpeningCash] = useState("");
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [quality, setQuality] = useState(null);

  const loadAssets = async () => {
    setAssetsError("");
    try {
      setAssets(await listAssets());
    } catch (err) {
      setAssetsError(err.message || "Could not load the asset catalog.");
    }
  };

  useEffect(() => {
    loadAssets();
  }, []);

  const stepIndex = STEPS.findIndex((s) => s.id === step);

  const createPortfolio = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const created = await createAccount(name.trim(), broker.trim());
      setAccount(created);
      setStep("baseline");
    } catch (err) {
      setError(err.message || "Could not create the portfolio.");
    } finally {
      setBusy(false);
    }
  };

  // Validation runs before anything is written, so "Review" shows real results
  // rather than a promise that the next click will work.
  const validate = async () => {
    setBusy(true);
    setError("");
    try {
      if (mode === "history") {
        if (!file) throw new Error("Choose a CSV file to import.");
        setPreview(await previewImport(account.id, file));
      } else {
        const entered = rows.filter((r) => r.assetKey && Number(r.quantity) > 0);
        if (!entered.length && !Number(openingCash)) {
          throw new Error("Enter at least one position or an opening cash balance.");
        }
        setPreview({
          valid: true,
          row_count: entered.length + (Number(openingCash) ? 1 : 0),
        });
      }
      setQuality(await accountDataQuality(account.id).catch(() => null));
      setStep("review");
    } catch (err) {
      setError(err.row ? `Row ${err.row}: ${err.message}` : err.message);
      setPreview(null);
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    setError("");
    try {
      if (mode === "history") {
        await commitImport(account.id, file);
      } else {
        // One timestamp for every opening entry: the ledger requires opening
        // rows to share the tracking start instant.
        const occurredAt = new Date().toISOString();
        if (Number(openingCash) > 0) {
          await createLedgerEntry(account.id, {
            kind: "opening_cash",
            amount_tomans: openingCash,
            occurred_at: occurredAt,
            note: "Opening baseline",
          });
        }
        for (const row of rows.filter((r) => r.assetKey && Number(r.quantity) > 0)) {
          await createLedgerEntry(account.id, {
            kind: "opening_position",
            asset_key: row.assetKey,
            quantity: row.quantity,
            occurred_at: occurredAt,
            note: "Opening baseline",
          });
        }
      }
      setQuality(await accountDataQuality(account.id).catch(() => null));
      setStep("confirm");
      await reload();
      setActive(account.id);
    } catch (err) {
      setError(err.row ? `Row ${err.row}: ${err.message}` : err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="onboarding" data-testid="onboarding">
      <ol className="onboarding-steps" aria-label="Onboarding progress">
        {STEPS.map((s, index) => (
          <li
            key={s.id}
            className={index === stepIndex ? "current" : index < stepIndex ? "done" : ""}
            aria-current={index === stepIndex ? "step" : undefined}
          >
            <span className="step-index">{index + 1}</span> {s.label}
          </li>
        ))}
      </ol>

      {error && (
        <div className="error" role="alert">
          {error}
          <button type="button" className="link" onClick={() => setError("")}>Dismiss</button>
        </div>
      )}

      {step === "portfolio" && (
        <form className="card" onSubmit={createPortfolio}>
          <h2>Create your first portfolio</h2>
          <p className="muted">
            A portfolio groups the positions and cash you want tracked and analysed
            together. You can add more later.
          </p>
          <label htmlFor="onboarding-name">Portfolio name</label>
          <input
            id="onboarding-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={120}
          />
          <label htmlFor="onboarding-broker">Broker (optional)</label>
          <input
            id="onboarding-broker"
            value={broker}
            onChange={(e) => setBroker(e.target.value)}
            maxLength={120}
          />
          <button type="submit" className="primary" disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Continue"}
          </button>
        </form>
      )}

      {step === "baseline" && (
        <div className="card">
          <h2>How much history do you have?</h2>
          <fieldset className="baseline-choice">
            <legend className="sr-only">Baseline type</legend>
            <label className={mode === "baseline" ? "choice selected" : "choice"}>
              <input
                type="radio"
                name="baseline-mode"
                value="baseline"
                checked={mode === "baseline"}
                onChange={() => setMode("baseline")}
              />
              <span>
                <strong>Start from today's positions</strong>
                <small>
                  Record what you hold now. No purchase price is invented, so
                  performance is measured from today forward and asset P&amp;L stays
                  labelled “cost basis unknown”.
                </small>
              </span>
            </label>
            <label className={mode === "history" ? "choice selected" : "choice"}>
              <input
                type="radio"
                name="baseline-mode"
                value="history"
                checked={mode === "history"}
                onChange={() => setMode("history")}
              />
              <span>
                <strong>Import my transaction history</strong>
                <small>
                  Upload a CSV of real events. Deposits and withdrawals become
                  external cash flows, so time- and money-weighted returns are
                  defensible from your first transaction.
                </small>
              </span>
            </label>
          </fieldset>
          <div className="inline">
            <button type="button" onClick={() => setStep("portfolio")}>Back</button>
            <button type="button" className="primary" onClick={() => setStep("entry")}>
              Continue
            </button>
          </div>
        </div>
      )}

      {step === "entry" && mode === "baseline" && (
        <div className="card">
          <h2>What do you hold today?</h2>
          {assetsError && (
            <div className="error" role="alert">
              {assetsError}
              <button type="button" className="link" onClick={loadAssets}>Retry</button>
            </div>
          )}
          <table className="onboarding-rows">
            <caption className="sr-only">Opening positions</caption>
            <thead>
              <tr>
                <th scope="col">Asset</th>
                <th scope="col">Quantity</th>
                <th scope="col"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>
                  <td>
                    <label className="sr-only" htmlFor={`row-asset-${index}`}>
                      Asset for row {index + 1}
                    </label>
                    <select
                      id={`row-asset-${index}`}
                      value={row.assetKey}
                      onChange={(e) => setRows((r) =>
                        r.map((item, i) => (i === index ? { ...item, assetKey: e.target.value } : item))
                      )}
                    >
                      <option value="">Select an asset…</option>
                      {assets.map((asset) => (
                        <option key={asset.key} value={asset.key}>{asset.name}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <label className="sr-only" htmlFor={`row-qty-${index}`}>
                      Quantity for row {index + 1}
                    </label>
                    <input
                      id={`row-qty-${index}`}
                      type="number"
                      min="0"
                      step="any"
                      value={row.quantity}
                      onChange={(e) => setRows((r) =>
                        r.map((item, i) => (i === index ? { ...item, quantity: e.target.value } : item))
                      )}
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      onClick={() => setRows((r) => (r.length === 1 ? [blankRow()] : r.filter((_, i) => i !== index)))}
                    >
                      Remove row {index + 1}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button type="button" onClick={() => setRows((r) => [...r, blankRow()])}>
            Add another position
          </button>
          <label htmlFor="opening-cash">Opening cash balance (Tomans)</label>
          <input
            id="opening-cash"
            type="number"
            min="0"
            step="any"
            value={openingCash}
            onChange={(e) => setOpeningCash(e.target.value)}
          />
          <div className="inline">
            <button type="button" onClick={() => setStep("baseline")}>Back</button>
            <button type="button" className="primary" onClick={validate} disabled={busy}>
              {busy ? "Validating…" : "Validate"}
            </button>
          </div>
        </div>
      )}

      {step === "entry" && mode === "history" && (
        <div className="card">
          <h2>Upload your transaction history</h2>
          <p className="muted">
            The columns are fixed. Preview validates every row and writes nothing;
            you confirm before anything is saved.
          </p>
          <pre className="csv-header">{CSV_HEADER}</pre>
          <p className="muted">
            <code>kind</code> is one of <code>opening_position</code>,{" "}
            <code>opening_cash</code>, <code>deposit</code>, <code>withdrawal</code>,{" "}
            <code>buy</code>, <code>sell</code>, <code>dividend</code>,{" "}
            <code>fee</code>. Rows may appear in any order — they are applied in
            event order.
          </p>
          <label htmlFor="ledger-csv">Ledger CSV</label>
          <input
            id="ledger-csv"
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => {
              setFile(e.target.files?.[0] || null);
              setPreview(null);
            }}
          />
          <div className="inline">
            <button type="button" onClick={() => setStep("baseline")}>Back</button>
            <button type="button" className="primary" onClick={validate} disabled={busy || !file}>
              {busy ? "Validating…" : "Preview import"}
            </button>
          </div>
        </div>
      )}

      {step === "review" && (
        <div className="card">
          <h2>Review before saving</h2>
          <p className="status-line" data-testid="review-valid">
            <span className="badge badge-success">Validated</span>{" "}
            {preview?.row_count} entr{preview?.row_count === 1 ? "y" : "ies"} passed
            every check. Nothing has been written yet.
          </p>
          <DataQualityPanel quality={quality} />
          <div className="inline">
            <button type="button" onClick={() => setStep("entry")}>Back</button>
            <button type="button" className="primary" onClick={confirm} disabled={busy}>
              {busy ? "Saving…" : mode === "history" ? "Confirm import" : "Confirm baseline"}
            </button>
          </div>
        </div>
      )}

      {step === "confirm" && (
        <div className="card" data-testid="onboarding-done">
          <h2>Your portfolio is ready</h2>
          <p>
            {mode === "history"
              ? "Your history is in the ledger. Performance runs from your first transaction."
              : "Your opening baseline is recorded. Performance runs from today; no earlier return is claimed."}
          </p>
          <DataQualityPanel quality={quality} />
          <button type="button" className="primary" onClick={() => navigate("/")}>
            Go to dashboard
          </button>
        </div>
      )}

      <p className="disclaimer">
        Lattice is an informational decision-support tool. It does not provide
        investment advice, and no figure here is a recommendation to buy or sell.
      </p>
    </div>
  );
}

export function DataQualityPanel({ quality }) {
  if (!quality) return null;
  if (!quality.assets?.length) {
    return <p className="muted">No priced assets to assess yet.</p>;
  }
  return (
    <div className="data-quality">
      <h3>Price data quality</h3>
      <p className="muted">
        {quality.passing_assets} of {quality.assessed_assets} assessed assets have
        trustworthy history ({quality.quality_status}).
      </p>
      <ul className="quality-list">
        {quality.assets.map((asset) => (
          <li key={asset.asset_key}>
            <span className={`badge ${qualityClass(asset)}`}>
              {asset.quality_status || (asset.passes_gate ? "ok" : "failing")}
            </span>{" "}
            <strong>{asset.asset_key}</strong>
            {asset.reason_codes?.length ? ` — ${asset.reason_codes.join(", ")}` : ""}
          </li>
        ))}
      </ul>
    </div>
  );
}

function qualityClass(asset) {
  if (asset.passes_gate === null) return "muted-badge";
  return asset.passes_gate ? "badge-success" : "badge-warn";
}

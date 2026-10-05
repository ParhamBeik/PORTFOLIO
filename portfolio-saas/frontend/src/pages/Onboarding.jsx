import { useRef, useState } from "react";
import { translate } from "../i18n.js";
import { useNavigate } from "react-router-dom";
import { addHolding, createAccount, listAssets } from "../api.js";
import { assetLabel } from "../format.js";
import { QUANTITY_MIN, quantityError, validQuantity } from "../quantity.js";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { useApi } from "../useApi.js";
import { Async, Button, Card, ErrorState, Input, Select } from "../components/ui.jsx";

// The dashboard has nothing to render until one holding exists, so this is the
// only route a new account can reach. It creates the portfolio and the first
// holding in one submit, then hands over to the dashboard for everything else.
export default function Onboarding() {
  const navigate = useNavigate();
  const { accounts, reload, setActive } = usePortfolio();
  const assets = useApi(listAssets, []);

  const [name, setName] = useState("My portfolio");
  const [assetKey, setAssetKey] = useState("");
  const [quantity, setQuantity] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // Reaching this route with an account already present means the account is
  // simply empty — reuse it rather than stacking a second one.
  const existing = accounts[0];
  // The account survives a failed holding. Only the successful path reloads the
  // portfolio context, so on a rejected quantity `accounts` was still empty on
  // the retry and the form opened a SECOND portfolio; three attempts left three.
  const created = useRef(null);

  const submit = async (e) => {
    e.preventDefault();
    const quantityMessage = quantityError(quantity);
    if (quantityMessage) {
      setError(new Error(quantityMessage));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const account =
        existing ||
        created.current ||
        (created.current = await createAccount(name.trim() || "My portfolio"));
      await addHolding(account.id, assetKey, quantity);
      setActive(account.id);
      await reload();
      navigate("/", { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-lg">
      <Card
        testId="onboarding-card"
        // "First" only when it is: this form is also the Add holdings tab of
        // a portfolio that already holds plenty.
        title={accounts.some((a) => (a.holdings || []).length) ? "Add a holding" : "Add your first holding"}
        subtitle="Your dashboard, optimizer and universe comparison all read from your holdings. One is enough to get started."
      >
        <form className="space-y-4" onSubmit={submit}>
          {!existing && (
            <label className="block">
              <span className="mb-1 block text-sm text-muted">Portfolio name</span>
              <Input
                label="Portfolio name"
                data-testid="onboarding-name"
                className="w-full"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </label>
          )}

          <div>
            <span className="mb-1 block text-sm text-muted">{translate("Asset")}</span>
            <Async {...assets} testId="onboarding-assets" empty="No assets are available yet.">
              {(list) => (
                <Select
                  label="Asset"
                  data-testid="onboarding-asset"
                  className="w-full"
                  value={assetKey}
                  onChange={(e) => setAssetKey(e.target.value)}
                  required
                >
                  <option value="">{translate("Select an asset…")}</option>
                  {list.map((a) => (
                    <option key={a.key} value={a.key}>{assetLabel(a)}</option>
                  ))}
                </Select>
              )}
            </Async>
          </div>

          <label className="block">
            <span className="mb-1 block text-sm text-muted">{translate("Quantity")}</span>
            <Input
              label="Quantity"
              data-testid="onboarding-quantity"
              className="w-full"
              type="number"
              step="any"
              min={QUANTITY_MIN}
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              required
            />
            {/* Say why, rather than leaving a dead Submit button and no reason
                for it — the same complaint the add-transaction wizard answered. */}
            {quantityError(quantity) && (
              <span className="mt-1 block text-xs text-critical" data-testid="onboarding-quantity-error">
                {quantityError(quantity)}
              </span>
            )}
          </label>

          {error && <ErrorState error={error} testId="onboarding-error" />}

          <Button
            variant="primary"
            type="submit"
            data-testid="onboarding-submit"
            disabled={busy || !assetKey || !validQuantity(quantity)}
            className="w-full"
          >
            {busy ? "Adding…" : "Add holding"}
          </Button>
        </form>
      </Card>
    </div>
  );
}

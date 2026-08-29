// The one way to put something into a portfolio.
//
// It replaced a form that asked the user to pick from Trade / Cash / Opening and
// then Buy / Sell / Deposit / Withdrawal / Opening cash / Opening position —
// eleven pieces of internal vocabulary, in two dropdowns, with nothing on screen
// explaining which combination meant what. This asks the same questions in the
// order a person actually knows the answers: what kind of thing, which one, what
// happened to it, how much, at what price — then shows the sentence it is about
// to record before recording it.
//
// Domain kinds never reach the screen. `ACTIONS` is the only place a plain-
// language choice is mapped onto a LedgerEntry kind.
import { useEffect, useMemo, useState } from "react";
import {
  addProperty,
  createLedgerEntry,
  ensureAsset,
  listAssets,
  searchAssetCatalog,
  trade,
  updateHolding,
} from "../api.js";
import { useApi } from "../useApi.js";
import { Badge, Button, ErrorState, Input, Loading, Modal, Select } from "./ui.jsx";
import { area, assetLabel, holdingLabel, perSqm, toman } from "../format.js";

// Mirrors SEARCH_LIMIT in backend/portfolio/services/catalog.py. Only used to
// decide whether to tell the user the list was cut short.
const CATALOG_PAGE_SIZE = 40;

// Asset classes as the user thinks of them, in the order they are usually held.
// `match` reads the catalog's `asset_class`, which is the API's own vocabulary.
const CATEGORIES = [
  { value: "Gold", label: "Gold & coins", hint: "Coins, bars, gold by the gram" },
  { value: "Cash", label: "Currency & cash", hint: "Dollars, euros, Tether" },
  { value: "Stock", label: "Stocks", hint: "Shares listed on the exchange" },
  { value: "Crypto", label: "Crypto", hint: "Coins and tokens" },
  { value: "Real Estate", label: "Property", hint: "A home, a shop, land" },
  { value: "__cash_move", label: "Money in or out", hint: "A deposit or a withdrawal" },
];

// Plain language on the left, the ledger's own vocabulary on the right.
const ACTIONS = {
  buy: { label: "I bought it", hint: "Adds to what you hold" },
  sell: { label: "I sold it", hint: "Reduces what you hold" },
  opening_position: {
    label: "I already own it",
    hint: "Records the position without a purchase",
  },
  valuation_mark: {
    label: "Its value changed",
    hint: "Records what it is worth now, dated",
  },
  deposit: { label: "Money in", hint: "Cash added to this portfolio" },
  withdrawal: { label: "Money out", hint: "Cash taken out" },
};

function actionsFor(asset, isNewProperty) {
  if (isNewProperty) return ["opening_position"];
  // Property is revalued, never traded — the backend refuses a house on the trade
  // endpoint, and selling one means removing the holding, which is a Holdings
  // action. Offering "I sold it" here would open a path that cannot complete.
  if (asset?.is_house) return ["valuation_mark"];
  if (asset?.is_manual) return ["buy", "sell", "opening_position"];
  return ["buy", "sell", "opening_position"];
}

// Which screens this particular entry needs. A cash movement has no asset to
// pick, so it does not show a step that would only ever be empty.
const stepsFor = (isCashMove) =>
  isCashMove
    ? ["category", "action", "amount", "review"]
    : ["category", "asset", "action", "amount", "review"];

/** A big, obvious choice tile — the step-1 and step-3 control. */
function Choice({ label, hint, selected, onClick, testId, disabled = false }) {
  return (
    <button
      type="button"
      onClick={onClick}
      // Picking a catalog row POSTs to mint the asset. Leaving the tile live
      // during that round trip let a double-click fire two creates for one
      // instrument, and the second came back a 500.
      disabled={disabled}
      aria-pressed={selected}
      data-testid={testId}
      className={`w-full rounded-lg border px-4 py-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
        selected
          ? "border-accent bg-accent/10"
          : "border-border bg-panel-2 hover:border-accent/50"
      }`}
    >
      <div className="text-sm font-medium">{label}</div>
      {hint && <div className="mt-0.5 text-xs text-muted">{hint}</div>}
    </button>
  );
}

function Step({ n, of, title, children }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Badge variant="neutral">
          Step {n} of {of}
        </Badge>
        <span className="text-sm font-medium">{title}</span>
      </div>
      {children}
    </div>
  );
}

const toIso = (local) => {
  if (!local) return null;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
};

const positive = (v) => v !== "" && Number(v) > 0;

// What the server will actually take: `DecimalField(max_digits=20,
// decimal_places=6, min_value=0.000001)`. The wizard only asked for "> 0", so a
// quantity of 0.0000001 passed every step, reached the review screen and was
// refused at Save -- the same shape as the future date that used to be caught
// only by the server. Stated here as one rule so the two cannot drift.
const QUANTITY_MIN = 0.000001;
const QUANTITY_MAX = 1e14; // max_digits 20 - decimal_places 6

const quantityError = (v) => {
  if (v === "") return "";
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return "Enter a quantity greater than zero.";
  if (n < QUANTITY_MIN) return `The smallest quantity we record is ${QUANTITY_MIN}.`;
  if (n >= QUANTITY_MAX) return "That quantity is larger than we can record.";
  if ((String(v).split(".")[1] || "").length > 6) return "At most 6 decimal places.";
  return "";
};

const validQuantity = (v) => positive(v) && !quantityError(v);

export default function AddTransactionDialog({
  accountId,
  accounts = [],
  onClose,
  onSaved,
  holdings = [],
}) {
  const assets = useApi(listAssets, []);
  const [step, setStep] = useState(0);
  const [category, setCategory] = useState("");
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [picked, setPicked] = useState(null);
  const [assetKey, setAssetKey] = useState("");
  const [newProperty, setNewProperty] = useState(false);
  const [action, setAction] = useState("");
  const [form, setForm] = useState({
    name: "",
    quantity: "",
    areaSqm: "",
    pricePerSqm: "",
    amount: "",
    price: "",
    when: "",
    note: "",
  });
  const [ownPrice, setOwnPrice] = useState(false);
  const [formAccount, setFormAccount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const targetAccountId = accountId ?? (formAccount ? Number(formAccount) : null);
  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), 200);
    return () => clearTimeout(t);
  }, [query]);

  const searchingCatalog = !!category && category !== "Real Estate" && category !== "__cash_move";
  const catalogHits = useApi(
    () => searchAssetCatalog(category, debounced),
    [category, debounced],
    { enabled: searchingCatalog },
  );

  const catalog = assets.data || [];
  const asset = picked || catalog.find((a) => a.key === assetKey) || null;
  const isCashMove = category === "__cash_move";
  const isProperty = category === "Real Estate";
  const holding = holdings.find(
    (h) => h.asset_key === assetKey && (h.account_id ?? accountId) === targetAccountId
  );

  // Properties are minted per holding, so the catalog carries every property the
  // user owns anywhere. Only the ones held in the portfolio being edited can be
  // revalued here -- the others have no holding row to write the mark against.
  const options = useMemo(() => {
    const inClass = catalog.filter((a) => a.asset_class === category);
    if (category === "Real Estate") {
      const held = new Set(
        holdings
          .filter((h) => (h.account_id ?? accountId) === targetAccountId)
          .map((h) => h.asset_key)
      );
      return inClass.filter((a) => held.has(a.key));
    }
    if (searchingCatalog && catalogHits.loading) return [];
    const remote = catalogHits.data;
    if (Array.isArray(remote)) return remote;
    return inClass;
  }, [catalog, category, holdings, accountId, targetAccountId, searchingCatalog, catalogHits.data, catalogHits.loading]);
  const available = actionsFor(asset, newProperty);

  // Manual assets have no feed, so their price is something only the user knows.
  // Everything else is priced from the market for the chosen date unless the user
  // explicitly overrides it.
  const priceIsMine = !!asset?.is_manual || ownPrice;
  // The exchange quotes stocks in Rial and the backend stores that verbatim;
  // every other asset is Toman. `tse_symbol` is the same discriminator the
  // server uses (marketdata.currency.is_tse_priced), so the two cannot drift.
  // Stocks are quoted in Rial on the exchange and stored that way; everything
  // else is Toman. Labelling both "Toman" made a user type a tenth of the real
  // price, and the cost basis came out 10x low against a Rial market price.
  const priceIsRial = !!asset?.tse_symbol;
  const priceUnitLabel = priceIsRial ? "Rial" : "Toman";
  // A manual asset is asked for its price even when nothing was traded: it has
  // no feed, so if the user does not state a value it has none at all and lands
  // in the portfolio worth nothing. Market-priced assets skip the question on
  // "I already own it" because the market answers it.
  const needsPrice =
    !isCashMove && !isProperty && !newProperty &&
    (action !== "opening_position" || !!asset?.is_manual);

  const steps = stepsFor(isCashMove);
  const current = steps[step];
  const stepNumber = step + 1;
  const totalSteps = steps.length;

  const reset = () => {
    setAssetKey("");
    setPicked(null);
    setQuery("");
    setDebounced("");
    setNewProperty(false);
    setAction("");
    setOwnPrice(false);
    setForm({
      name: "", quantity: "", areaSqm: "", pricePerSqm: "",
      amount: "", price: "", when: "", note: "",
    });
  };

  const pickCategory = (value) => {
    setCategory(value);
    reset();
    setError(null);
    // Advancing by name rather than by number: a cash movement's second screen is
    // the action, everything else's is the asset picker.
    setStep(1);
  };

  const pickAsset = (key, asNewProperty = false, row = null) => {
    setAssetKey(key);
    setPicked(row && row.key === key ? row : null);
    setNewProperty(asNewProperty);
    const next = actionsFor(row || catalog.find((a) => a.key === key), asNewProperty);
    setAction(next[0]);
    setStep(steps.indexOf("action"));
  };

  const chooseRow = async (row) => {
    if (row.key) {
      pickAsset(row.key, false, row);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await ensureAsset(row.source, row.symbol);
      pickAsset(created.key, false, created);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const canContinue = () => {
    if (current === "asset") return !!assetKey || newProperty;
    if (current === "action") return !!action;
    if (current === "amount") {
      if (isCashMove) return positive(form.amount);
      if (newProperty) {
        return !!form.name.trim() && positive(form.areaSqm) && positive(form.pricePerSqm);
      }
      if (asset?.is_house) return positive(form.pricePerSqm);
      return validQuantity(form.quantity);
    }
    return true;
  };

  const canSave = () => {
    if (!targetAccountId) return false;
    // A revaluation writes to a holding row; without one there is nothing to mark.
    if (asset?.is_house && !newProperty && !holding) return false;
    return !needsPrice || !priceIsMine || positive(form.price);
  };

  const summary = () => {
    const when = form.when ? new Date(form.when).toLocaleString("en-GB") : "now";
    const name = newProperty ? form.name || "the property" : holdingLabel(asset || {});
    if (isCashMove) {
      const verb = action === "deposit" ? "Add" : "Take out";
      return `${verb} ${toman(form.amount)} — ${when}.`;
    }
    if (isProperty || newProperty || asset?.is_house) {
      const sqm = form.areaSqm || holding?.area_sqm;
      const value = Number(sqm || 0) * Number(form.pricePerSqm || 0) * 1e6;
      return `${name}: ${area(sqm)} at ${perSqm(
        Number(form.pricePerSqm || 0) * 1e6
      )} — ${toman(value)}, as of ${when}.`;
    }
    const verb = { buy: "Buy", sell: "Sell", opening_position: "Record" }[action];
    // A stock's unit price is Rial, so the total is Rial/10. Printing both with
    // toman() overstated a stock purchase tenfold on the confirmation screen.
    const lineTotal = priceIsRial
      ? (Number(form.quantity) * Number(form.price)) / 10
      : Number(form.quantity) * Number(form.price);
    const priced = priceIsMine && form.price
      ? ` at ${Number(form.price).toLocaleString()} ${priceUnitLabel} each — ${toman(lineTotal)}`
      : " at the market price for that date";
    if (action === "opening_position") {
      return `${verb} that you already hold ${form.quantity} ${name} — as of ${when}.`;
    }
    return `${verb} ${form.quantity} ${name}${priced} — ${when}.`;
  };

  const submit = async () => {
    if (!targetAccountId || busy) return;
    setBusy(true);
    setError(null);
    const occurredAt = toIso(form.when);
    try {
      if (isCashMove) {
        await createLedgerEntry(targetAccountId, {
          kind: action,
          amount_tomans: form.amount,
          note: form.note,
          ...(occurredAt ? { occurred_at: occurredAt } : {}),
        });
      } else if (newProperty) {
        await addProperty(targetAccountId, {
          name: form.name,
          areaSqm: form.areaSqm,
          pricePerSqmMillion: form.pricePerSqm,
          ...(occurredAt ? { occurredAt } : {}),
        });
      } else if (asset?.is_house && action === "valuation_mark") {
        // A revaluation is a dated mark on the holding, not a trade — houses are
        // not tradeable and the backend refuses them on the trade endpoint.
        await updateHolding(targetAccountId, holding.id, {
          pricePerSqmMillion: form.pricePerSqm,
          areaSqm: form.areaSqm || holding.area_sqm,
          ...(occurredAt ? { occurredAt } : {}),
        });
      } else if (action === "opening_position" || action === "valuation_mark") {
        await createLedgerEntry(targetAccountId, {
          kind: "opening_position",
          asset_key: assetKey,
          quantity: form.quantity,
          note: form.note,
          ...(priceIsMine && form.price ? { unit_price_tomans: form.price } : {}),
          ...(occurredAt ? { occurred_at: occurredAt } : {}),
        });
      } else {
        await trade(targetAccountId, {
          assetKey,
          side: action,
          quantity: form.quantity,
          note: form.note,
          timestamp: occurredAt,
          priceTomans: priceIsMine ? form.price : null,
        });
      }
      await onSaved?.();
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const back = () => setStep((s) => Math.max(0, s - 1));

  return (
    <Modal
      title="Add to this portfolio"
      subtitle="Record something you bought, sold, or already own."
      onClose={onClose}
      testId="add-transaction"
      size="wide"
      footer={
        <>
          <Button onClick={back} disabled={step === 0 || busy} data-testid="add-transaction-back">
            Back
          </Button>
          {current !== "review" ? (
            <Button
              variant="primary"
              disabled={!canContinue() || busy}
              onClick={() => setStep((s) => s + 1)}
              data-testid="add-transaction-next"
            >
              Continue
            </Button>
          ) : (
            <Button
              variant="primary"
              disabled={busy || !canSave()}
              onClick={submit}
              data-testid="add-transaction-save"
            >
              {busy ? "Saving…" : "Save"}
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-4">
        {error && <ErrorState error={error} testId="add-transaction-error" />}

        {accountId == null && (
          <Select
            label="Portfolio"
            data-testid="add-transaction-account"
            value={formAccount}
            onChange={(e) => setFormAccount(e.target.value)}
            className="w-full"
          >
            <option value="">Which portfolio?</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </Select>
        )}

        {current === "category" && (
          <Step n={stepNumber} of={totalSteps} title="What kind of thing is it?">
            <div className="grid gap-2 sm:grid-cols-2">
              {CATEGORIES.map((c) => (
                <Choice
                  key={c.value}
                  label={c.label}
                  hint={c.hint}
                  selected={category === c.value}
                  onClick={() => pickCategory(c.value)}
                  testId={`add-transaction-category-${c.value.replace(/\W+/g, "-").toLowerCase()}`}
                />
              ))}
            </div>
          </Step>
        )}

        {current === "asset" && (
          <Step n={stepNumber} of={totalSteps} title="Which one?">
            {searchingCatalog && (
              <Input
                label="Search the catalog"
                placeholder="Type a ticker or name"
                className="w-full"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                data-testid="add-transaction-asset-search"
              />
            )}
            {(assets.loading || (searchingCatalog && catalogHits.loading)) && <Loading />}
            {searchingCatalog && catalogHits.error && (
              <ErrorState
                error={catalogHits.error}
                onRetry={catalogHits.reload}
                testId="add-transaction-catalog-error"
              />
            )}
            {/* No scroller of its own: Modal's body already scrolls, and
                nesting a second one here left ~3 visible rows behind two
                scrollbars, with the search box sliding away from its results.
                Two columns because these rows are short and the list is long. */}
            <div className="grid gap-2 sm:grid-cols-2">
              {isProperty && (
                <Choice
                  label="Add a new property"
                  hint="Give it a name, its size, and the price per square meter"
                  selected={newProperty}
                  onClick={() => pickAsset("", true)}
                  testId="add-transaction-new-property"
                />
              )}
              {options.map((a) => (
                <Choice
                  key={a.key || `${a.source}:${a.symbol}`}
                  label={assetLabel(a)}
                  hint={
                    a.is_manual
                      ? "You set the price yourself"
                      : a.symbol
                        ? `${a.symbol} — priced from the market`
                        : "Priced from the market"
                  }
                  selected={!!a.key && assetKey === a.key}
                  onClick={() => chooseRow(a)}
                  disabled={busy}
                  testId={`add-transaction-asset-${a.key || `${a.source}-${a.symbol}`}`}
                />
              ))}
              {!assets.loading && !(searchingCatalog && catalogHits.loading) && !options.length && !isProperty && (
                <p className="text-sm text-muted" data-testid="add-transaction-asset-empty">
                  {debounced ? "Nothing matches that search." : "Nothing in this category yet."}
                </p>
              )}
            </div>
            {/* The catalog is thousands of tickers and the response is capped.
                Without saying so, a full page reads as "this is everything"
                and the user concludes their stock is not supported. */}
            {searchingCatalog && options.length >= CATALOG_PAGE_SIZE && (
              <p className="text-xs text-muted" data-testid="add-transaction-asset-truncated">
                Showing the first {CATALOG_PAGE_SIZE} matches. Type a ticker or
                name to narrow it down.
              </p>
            )}
          </Step>
        )}

        {current === "action" && (
          <Step n={stepNumber} of={totalSteps} title="What happened?">
            <div className="space-y-2">
              {(isCashMove ? ["deposit", "withdrawal"] : available).map((k) => (
                <Choice
                  key={k}
                  label={ACTIONS[k].label}
                  hint={ACTIONS[k].hint}
                  selected={action === k}
                  onClick={() => setAction(k)}
                  testId={`add-transaction-action-${k}`}
                />
              ))}
            </div>
          </Step>
        )}

        {current === "amount" && (
          <Step n={stepNumber} of={totalSteps} title="How much?">
            <div className="space-y-3">
              {newProperty && (
                <Field label="What do you call it?">
                  <Input
                    label="Property name"
                    placeholder="Home"
                    className="w-full"
                    value={form.name}
                    onChange={set("name")}
                    data-testid="add-transaction-property-name"
                  />
                </Field>
              )}
              {isCashMove ? (
                <Field label="Amount (Toman)">
                  <Input
                    label="Amount"
                    type="number"
                    step="any"
                    className="w-full"
                    value={form.amount}
                    onChange={set("amount")}
                    data-testid="add-transaction-amount"
                  />
                </Field>
              ) : isProperty || newProperty || asset?.is_house ? (
                <>
                  <Field label="Size (square meters)">
                    <Input
                      label="Area"
                      type="number"
                      step="any"
                      placeholder="91"
                      className="w-full"
                      value={form.areaSqm || holding?.area_sqm || ""}
                      onChange={set("areaSqm")}
                      data-testid="add-transaction-area"
                    />
                  </Field>
                  <Field label="Price per square meter (millions of Toman)">
                    <Input
                      label="Price per square meter"
                      type="number"
                      step="any"
                      placeholder="100"
                      className="w-full"
                      value={form.pricePerSqm}
                      onChange={set("pricePerSqm")}
                      data-testid="add-transaction-price-per-sqm"
                    />
                  </Field>
                  {positive(form.pricePerSqm) && (
                    <p className="text-sm text-muted" data-testid="add-transaction-property-total">
                      {area(form.areaSqm || holding?.area_sqm)} × {perSqm(Number(form.pricePerSqm) * 1e6)}{" "}
                      ={" "}
                      <strong className="text-text">
                        {toman(
                          Number(form.areaSqm || holding?.area_sqm || 0) *
                            Number(form.pricePerSqm) * 1e6
                        )}
                      </strong>
                    </p>
                  )}
                </>
              ) : (
                <Field label="How many?">
                  <Input
                    label="Quantity"
                    type="number"
                    step="any"
                    min={QUANTITY_MIN}
                    className="w-full"
                    value={form.quantity}
                    onChange={set("quantity")}
                    data-testid="add-transaction-quantity"
                  />
                  {quantityError(form.quantity) && (
                    <p
                      className="mt-1 text-xs text-critical"
                      data-testid="add-transaction-quantity-error"
                    >
                      {quantityError(form.quantity)}
                    </p>
                  )}
                </Field>
              )}
              <Field label="When? (leave blank for now)">
                <Input
                  label="Date"
                  type="datetime-local"
                  className="w-full"
                  value={form.when}
                  onChange={set("when")}
                  data-testid="add-transaction-when"
                />
              </Field>
            </div>
          </Step>
        )}

        {current === "review" && (
          <Step n={stepNumber} of={totalSteps} title="Check it over">
            <div className="space-y-3">
              {needsPrice && (
                <>
                  {!asset?.is_manual && (
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={ownPrice}
                        onChange={(e) => setOwnPrice(e.target.checked)}
                        data-testid="add-transaction-own-price"
                      />
                      I paid a different price than the market
                    </label>
                  )}
                  {priceIsMine ? (
                    <Field label={`Price for one (${priceUnitLabel})`}>
                      <Input
                        label="Unit price"
                        type="number"
                        step="any"
                        className="w-full"
                        value={form.price}
                        onChange={set("price")}
                        data-testid="add-transaction-price"
                      />
                    </Field>
                  ) : (
                    <p className="text-sm text-muted">
                      The market price for that date will be used.
                    </p>
                  )}
                </>
              )}
              <Field label="Note (optional)">
                <Input
                  label="Note"
                  className="w-full"
                  value={form.note}
                  onChange={set("note")}
                  data-testid="add-transaction-note"
                />
              </Field>
              <div
                className="rounded-lg border border-border bg-panel-2 px-4 py-3 text-sm"
                data-testid="add-transaction-summary"
              >
                {summary()}
              </div>
            </div>
          </Step>
        )}
      </div>
    </Modal>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <div className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">{label}</div>
      {children}
    </div>
  );
}

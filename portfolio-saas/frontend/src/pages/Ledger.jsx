import { useState } from "react";
import {
  createLedgerEntry,
  listAssets,
  listLedger,
  reverseLedgerEntry,
} from "../api.js";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import {
  Async,
  Button,
  Card,
  Empty,
  Input,
  PageHeader,
  Select,
  Table,
} from "../components/ui.jsx";
import { assetLabel, dateTime, num, toman } from "../format.js";
import { useApi } from "../useApi.js";

const KINDS = [
  ["opening_position", "Opening position"],
  ["opening_cash", "Opening cash"],
  ["deposit", "Deposit"],
  ["withdrawal", "Withdrawal"],
  ["buy", "Buy"],
  ["sell", "Sell"],
  ["dividend", "Dividend"],
  ["fee", "Fee"],
];

const NEEDS_ASSET = new Set(["opening_position", "buy", "sell", "dividend"]);
const NEEDS_QTY = new Set(["opening_position", "buy", "sell"]);
const NEEDS_PRICE = new Set(["buy", "sell"]);
const NEEDS_AMOUNT = new Set(["opening_cash", "deposit", "withdrawal", "dividend", "fee"]);

export default function Ledger() {
  const { accounts, activeId, setActive, reload } = usePortfolio();
  const accountId = activeId ?? accounts[0]?.id ?? null;
  const assets = useApi(listAssets, []);
  const ledger = useApi(() => listLedger(accountId), [accountId], { enabled: accountId != null });

  const [kind, setKind] = useState("opening_position");
  const [assetKey, setAssetKey] = useState("");
  const [quantity, setQuantity] = useState("");
  const [unitPrice, setUnitPrice] = useState("");
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    if (!accountId || busy) return;
    setBusy(true);
    setError("");
    try {
      const body = { kind, note };
      if (NEEDS_ASSET.has(kind)) body.asset_key = assetKey;
      if (NEEDS_QTY.has(kind)) body.quantity = quantity;
      if (NEEDS_PRICE.has(kind)) body.unit_price_tomans = unitPrice;
      if (NEEDS_AMOUNT.has(kind)) body.amount_tomans = amount;
      await createLedgerEntry(accountId, body);
      await Promise.all([ledger.reload?.() ?? Promise.resolve(), reload()]);
      setQuantity("");
      setUnitPrice("");
      setAmount("");
      setNote("");
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const reverse = async (id) => {
    if (!accountId) return;
    setBusy(true);
    setError("");
    try {
      await reverseLedgerEntry(accountId, id);
      await Promise.all([ledger.reload?.() ?? Promise.resolve(), reload()]);
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!accounts.length) {
    return <Empty testId="ledger-empty">Create a portfolio first.</Empty>;
  }

  return (
    <div>
      <PageHeader
        title="Ledger"
        subtitle="Opening cash and positions unlock TWR, XIRR, cost basis, and P&L. Corrections append a reversal."
      />
      <div className="mb-4 max-w-sm">
        <Select
          label="Portfolio"
          data-testid="ledger-account"
          value={accountId ?? ""}
          onChange={(e) => setActive(Number(e.target.value))}
        >
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </Select>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Add entry" testId="ledger-form-card">
          <form className="space-y-3" onSubmit={submit} data-testid="ledger-form">
            {error && (
              <p role="alert" className="text-sm text-[var(--c-critical)]">{error}</p>
            )}
            <Select label="Kind" data-testid="ledger-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
              {KINDS.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </Select>
            {NEEDS_ASSET.has(kind) && (
              <Async {...assets} testId="ledger-assets">
                {(list) => (
                  <Select
                    label="Asset"
                    data-testid="ledger-asset"
                    value={assetKey}
                    onChange={(e) => setAssetKey(e.target.value)}
                    required
                  >
                    <option value="">Select an asset…</option>
                    {list.map((a) => (
                      <option key={a.key} value={a.key}>{assetLabel(a)}</option>
                    ))}
                  </Select>
                )}
              </Async>
            )}
            {NEEDS_QTY.has(kind) && (
              <Input
                label="Quantity"
                type="number"
                step="any"
                required
                data-testid="ledger-quantity"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
              />
            )}
            {NEEDS_PRICE.has(kind) && (
              <Input
                label="Unit price (Toman)"
                type="number"
                step="any"
                required
                data-testid="ledger-price"
                value={unitPrice}
                onChange={(e) => setUnitPrice(e.target.value)}
              />
            )}
            {NEEDS_AMOUNT.has(kind) && (
              <Input
                label="Amount (Toman)"
                type="number"
                step="any"
                required
                data-testid="ledger-amount"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
              />
            )}
            <Input
              label="Note"
              data-testid="ledger-note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <Button type="submit" variant="primary" disabled={busy} data-testid="ledger-submit">
              {busy ? "Saving…" : "Record entry"}
            </Button>
          </form>
        </Card>

        <Card title="History" testId="ledger-history-card">
          <Async {...ledger} testId="ledger-history" empty="No entries yet.">
            {(rows) => (
              <Table
                testId="ledger-table"
                rowKey={(r) => r.id}
                rows={rows}
                columns={[
                  { key: "when", header: "When", render: (r) => dateTime(r.occurred_at) },
                  { key: "kind", header: "Kind", render: (r) => r.kind },
                  { key: "asset", header: "Asset", render: (r) => r.asset_key || "—" },
                  { key: "qty", header: "Qty", align: "right", render: (r) => num(r.quantity, 4) },
                  { key: "amt", header: "Amount", align: "right", render: (r) => toman(r.amount_tomans) },
                  {
                    key: "rev",
                    header: "",
                    align: "right",
                    render: (r) =>
                      r.reversal_of ? null : (
                        <Button variant="ghost" disabled={busy} onClick={() => reverse(r.id)} data-testid="ledger-reverse">
                          Reverse
                        </Button>
                      ),
                  },
                ]}
              />
            )}
          </Async>
        </Card>
      </div>
    </div>
  );
}

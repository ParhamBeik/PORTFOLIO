import { useState } from "react";
import {
  createLedgerEntry,
  deleteLedgerEntry,
  deleteLedgerHolding,
  listAssets,
  listLedger,
  trade,
  updateLedgerEntry,
  updateLedgerHolding,
} from "../api.js";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import {
  Async,
  Badge,
  Button,
  Card,
  Delta,
  Empty,
  Input,
  Loading,
  PageHeader,
  Select,
  Table,
  Tabs,
} from "../components/ui.jsx";
import { assetLabel, dateTime, humanize, signedToman, toman } from "../format.js";
import { useApi } from "../useApi.js";

const TRADE_SIDES = [
  ["buy", "Buy"],
  ["sell", "Sell"],
];
const CASH_KINDS = [
  ["deposit", "Deposit"],
  ["withdrawal", "Withdrawal"],
];
const OPENING_KINDS = [
  ["opening_cash", "Opening cash"],
  ["opening_position", "Opening position"],
];

const qtyInputClass = "w-24 rounded-md border border-border bg-panel px-2 py-1 text-right text-sm tabular";

function PlusIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M10 4v12M4 10h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function kindBadge(row) {
  if (row.kind === "buy") return <Badge variant="good">Buy</Badge>;
  if (row.kind === "sell") return <Badge variant="critical">Sell</Badge>;
  if (row.kind === "position") return <Badge variant="neutral">Holding</Badge>;
  return <Badge variant="neutral">{humanize(row.kind)}</Badge>;
}

function toIso(local) {
  if (!local) return null;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export default function Ledger() {
  const { accounts, activeId, reload, loading: accountsLoading } = usePortfolio();
  const accountId = activeId ?? null;
  const assets = useApi(listAssets, []);
  const ledger = useApi(() => listLedger(accountId), [accountId], { enabled: accounts.length > 0 });

  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState("trade");
  const [kind, setKind] = useState("buy");
  const [formAccount, setFormAccount] = useState("");
  const [assetKey, setAssetKey] = useState("");
  const [quantity, setQuantity] = useState("");
  const [amount, setAmount] = useState("");
  const [when, setWhen] = useState("");
  const [note, setNote] = useState("");
  const [drafts, setDrafts] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const targetAccountId = accountId ?? (formAccount ? Number(formAccount) : null);

  const resetForm = () => {
    setQuantity("");
    setAmount("");
    setWhen("");
    setNote("");
    setError("");
  };

  const switchTab = (next) => {
    setTab(next);
    setKind(next === "trade" ? "buy" : next === "cash" ? "deposit" : "opening_cash");
    resetForm();
  };

  const refresh = () => Promise.all([ledger.reload?.() ?? Promise.resolve(), reload()]);

  const submit = async (e) => {
    e.preventDefault();
    if (!targetAccountId || busy) return;
    setBusy(true);
    setError("");
    const occurredAt = toIso(when);
    try {
      if (tab === "trade") {
        await trade(targetAccountId, {
          assetKey,
          side: kind,
          quantity,
          note,
          timestamp: occurredAt,
        });
      } else if (tab === "cash" || kind === "opening_cash") {
        const body = { kind, amount_tomans: amount, note };
        if (occurredAt) body.occurred_at = occurredAt;
        await createLedgerEntry(targetAccountId, body);
      } else {
        const body = { kind, asset_key: assetKey, quantity, note };
        if (occurredAt) body.occurred_at = occurredAt;
        await createLedgerEntry(targetAccountId, body);
      }
      await refresh();
      resetForm();
      setOpen(false);
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const saveRow = async (row) => {
    const qty = (drafts[row.id] ?? row.quantity ?? "").toString().trim();
    if (!qty || busy) return;
    setBusy(true);
    setError("");
    try {
      if (row.is_synthetic) {
        await updateLedgerHolding(row.account_id, row.holding_id, qty);
      } else {
        await updateLedgerEntry(row.account_id, row.id, { quantity: qty });
      }
      setDrafts((cur) => {
        const next = { ...cur };
        delete next[row.id];
        return next;
      });
      await refresh();
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const removeRow = async (row) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      if (row.is_synthetic) {
        await deleteLedgerHolding(row.account_id, row.holding_id);
      } else {
        await deleteLedgerEntry(row.account_id, row.id);
      }
      await refresh();
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  // Accounts start as [] while the list is still in flight (PortfolioContext),
  // so this must wait for `loading` to clear before deciding there really is
  // no portfolio -- otherwise every navigation here flashes the wrong empty
  // state for however long the account list takes to arrive.
  if (accountsLoading) {
    return <Loading testId="ledger-loading" />;
  }
  if (!accounts.length) {
    return <Empty testId="ledger-empty">Create a portfolio first.</Empty>;
  }

  const needsAsset = tab === "trade" || (tab === "opening" && kind === "opening_position");
  const needsQty = tab === "trade" || (tab === "opening" && kind === "opening_position");
  const needsAmount = tab === "cash" || (tab === "opening" && kind === "opening_cash");
  const kindOptions = tab === "trade" ? TRADE_SIDES : tab === "cash" ? CASH_KINDS : OPENING_KINDS;
  const showPortfolio = accountId == null;

  const columns = [
    { key: "when", header: "When", render: (r) => dateTime(r.occurred_at) },
    { key: "kind", header: "Side", render: kindBadge },
    {
      key: "asset",
      header: "Asset",
      render: (r) => (r.asset_key ? assetLabel({
        name_fa: r.asset_name_fa, name: r.asset_name, key: r.asset_key,
      }) : "—"),
    },
    {
      key: "qty",
      header: "Qty",
      align: "right",
      render: (r) => (
        r.quantity == null ? "—" : (
          <input
            type="number"
            step="any"
            className={qtyInputClass}
            aria-label={`Quantity for ${r.asset_key || r.kind}`}
            data-testid="ledger-edit-qty"
            disabled={busy}
            // The backend serializes quantities at each asset class's own
            // decimal precision ("100000.0" for shares, "4.000000" for
            // coins) -- round-tripping through Number() strips the
            // inconsistent trailing zeros for display without touching the
            // value the user is actively editing.
            value={drafts[r.id] ?? Number(r.quantity)}
            onChange={(e) => setDrafts((cur) => ({ ...cur, [r.id]: e.target.value }))}
          />
        )
      ),
    },
    {
      key: "price",
      header: "Price",
      align: "right",
      render: (r) => toman(r.unit_price_tomans),
    },
    { key: "amt", header: "Amount", align: "right", render: (r) => toman(r.amount_tomans) },
    {
      key: "pnl",
      header: "P/L",
      align: "right",
      render: (r) => (
        <span data-testid="ledger-pnl">
          <Delta value={r.pnl_tomans == null ? null : Number(r.pnl_tomans)} format={signedToman} />
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <div className="flex justify-end gap-2">
          {r.quantity != null && (
            <Button
              variant="ghost"
              disabled={busy || drafts[r.id] == null || drafts[r.id] === String(r.quantity)}
              onClick={() => saveRow(r)}
              data-testid="ledger-save"
            >
              Save
            </Button>
          )}
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => removeRow(r)}
            data-testid="ledger-delete"
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  if (showPortfolio) {
    columns.splice(2, 0, {
      key: "portfolio",
      header: "Portfolio",
      render: (r) => r.account_name || "—",
    });
  }

  return (
    <div>
      <PageHeader
        title="Ledger"
        subtitle="Add, edit, or delete buys and sells. Holdings with no trade history still appear so you can set their quantity. A sell cannot leave holdings negative at any point on the timeline."
        actions={
          <Button
            type="button"
            variant="primary"
            onClick={() => { setOpen((v) => !v); setError(""); }}
            data-testid="ledger-add"
            className="inline-flex items-center gap-1.5"
            aria-expanded={open}
            aria-label="Add transaction"
          >
            <PlusIcon />
            Add
          </Button>
        }
      />

      {error && !open && (
        <p role="alert" className="mb-3 text-sm text-[var(--c-critical)]">{error}</p>
      )}

      {open && (
        <Card title="Add transaction" testId="ledger-form-card" className="mb-5">
          <form className="space-y-3" onSubmit={submit} data-testid="ledger-form">
            {error && (
              <p role="alert" className="text-sm text-[var(--c-critical)]">{error}</p>
            )}
            {accountId == null && (
              <Select
                label="Portfolio"
                data-testid="ledger-account"
                value={formAccount}
                onChange={(e) => setFormAccount(e.target.value)}
                required
              >
                <option value="">Select a portfolio…</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>{a.name}</option>
                ))}
              </Select>
            )}
            <Tabs
              label="Entry type"
              testId="ledger-tabs"
              value={tab}
              onChange={switchTab}
              options={[
                { value: "trade", label: "Buy / Sell" },
                { value: "cash", label: "Cash" },
                { value: "opening", label: "Opening" },
              ]}
            />
            <Select
              label="Kind"
              data-testid="ledger-kind"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
            >
              {kindOptions.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </Select>
            {needsAsset && (
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
                    {list.filter((a) => !a.is_house).map((a) => (
                      <option key={a.key} value={a.key}>{assetLabel(a)}</option>
                    ))}
                  </Select>
                )}
              </Async>
            )}
            {needsQty && (
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
            {needsAmount && (
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
              label="Date (optional)"
              type="datetime-local"
              data-testid="ledger-when"
              value={when}
              onChange={(e) => setWhen(e.target.value)}
            />
            <Input
              label="Note"
              data-testid="ledger-note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <div className="flex gap-2">
              <Button type="submit" variant="primary" disabled={busy} data-testid="ledger-submit">
                {busy ? "Saving…" : "Record"}
              </Button>
              <Button type="button" disabled={busy} onClick={() => { setOpen(false); resetForm(); }}>
                Cancel
              </Button>
            </div>
          </form>
        </Card>
      )}

      <Card title="History" testId="ledger-history-card">
        <Async {...ledger} testId="ledger-history" empty="No entries or holdings yet. Use + to add a buy or sell.">
          {(rows) => (
            <Table
              testId="ledger-table"
              rowKey={(r) => r.id}
              rows={rows}
              columns={columns}
            />
          )}
        </Async>
      </Card>
    </div>
  );
}

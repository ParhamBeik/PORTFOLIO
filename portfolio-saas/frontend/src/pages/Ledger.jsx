import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  commitLedgerImport,
  deleteLedgerEntry,
  deleteLedgerHolding,
  listLedger,
  restoreLedgerEntry,
  previewLedgerImport,
  updateLedgerEntry,
  updateLedgerHolding,
} from "../api.js";
import AddTransactionDialog from "../components/AddTransactionDialog.jsx";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import {
  Async,
  Badge,
  Button,
  Card,
  Delta,
  Empty,
  ErrorState,
  Input,
  Loading,
  Field,
  JalaliDateField,
  Modal,
  PageHeader,
  Pager,
  Select,
  Table,
} from "../components/ui.jsx";
import {
  area,
  dateTime,
  holdingLabel,
  jalaliDate,
  humanize,
  perSqm,
  signedToman,
  toman,
  unitPrice,
  isolate,
} from "../format.js";
import { useApi } from "../useApi.js";
import { quantityError, validQuantity } from "../quantity.js";

function ImportCsvDialog({ accountId, onClose, onImported }) {
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const inspect = async () => {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      setPreview(await previewLedgerImport(accountId, file));
    } catch (e) {
      setError(e.message || String(e));
      setPreview(null);
    } finally {
      setBusy(false);
    }
  };

  const commit = async () => {
    if (!file || !preview?.valid) return;
    setBusy(true);
    setError("");
    try {
      const result = await commitLedgerImport(accountId, file);
      setDone(true);
      await onImported?.();
      if (!result.row_count) setError("The import completed without any rows.");
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="Import transaction history"
      subtitle="Upload the CSV exported from your broker, review it, then commit it as ledger history."
      onClose={onClose}
      testId="ledger-import-dialog"
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Close</Button>
          {!done && (
            <Button
              variant="primary"
              disabled={!preview?.valid || busy}
              onClick={commit}
              data-testid="ledger-import-commit"
            >
              {busy ? "Importing…" : "Import rows"}
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-4">
        {error && <ErrorState error={new Error(error)} testId="ledger-import-error" />}
        {done ? (
          <p className="text-sm text-[var(--c-good-text)]" data-testid="ledger-import-success">
            Import complete. The same file can be selected again safely; duplicate files are ignored.
          </p>
        ) : (
          <>
            <label className="block text-sm">
              <span className="mb-1 block text-xs font-medium tracking-wide text-muted uppercase">
                CSV file
              </span>
              <input
                type="file"
                accept=".csv,text/csv"
                onChange={(e) => {
                  setFile(e.target.files?.[0] || null);
                  setPreview(null);
                  setError("");
                }}
                data-testid="ledger-import-file"
                className="block w-full rounded-md border border-border bg-panel-2 px-3 py-2 text-sm"
              />
            </label>
            <p className="text-xs text-muted">
              Required columns: external_id, occurred_at, kind, asset_key, quantity,
              unit_price_tomans, amount_tomans, note. Dates must be ISO-8601; TSE stock
              unit prices stay in Rial, and all other unit prices use Toman.
            </p>
            <Button
              variant="secondary"
              disabled={!file || busy}
              onClick={inspect}
              data-testid="ledger-import-preview"
            >
              {busy ? "Checking…" : "Validate file"}
            </Button>
            {preview?.valid && (
              <p className="text-sm" data-testid="ledger-import-preview-result">
                {preview.row_count} rows are valid and ready to import.
              </p>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}

// Plain-language names for the ledger's own vocabulary. The page never shows a
// kind string: "opening_position" told the user nothing about what they did.
const KIND_LABEL = {
  buy: "Bought",
  sell: "Sold",
  opening_position: "Already owned",
  opening_cash: "Starting cash",
  deposit: "Money in",
  withdrawal: "Money out",
  dividend: "Dividend",
  fee: "Fee",
  valuation_mark: "Revalued",
  position: "Owned",
};

// A full history is thousands of pixels of table. 25 keeps the card about one
// screen tall; "All" is still there for anyone scanning the whole thing.
const ALL = "all";
const PAGE_SIZES = [10, 25, 50, 100, ALL];

function PlusIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M10 4v12M4 10h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function kindBadge(row) {
  const label = KIND_LABEL[row.kind] || humanize(row.kind);
  if (row.kind === "buy") return <Badge variant="good">{label}</Badge>;
  if (row.kind === "sell") return <Badge variant="critical">{label}</Badge>;
  if (row.is_synthetic) {
    return (
      <Badge variant="warn" title="This holding has no purchase recorded, so it has no cost basis.">
        {label} — no purchase recorded
      </Badge>
    );
  }
  return <Badge variant="neutral">{label}</Badge>;
}

/**
 * How much of the asset the row moved.
 *
 * A property's quantity is a price per square meter in millions of Toman, not a
 * count of anything, so printing the raw number ("100") was meaningless. Its size
 * is the "how much", and the price per square meter belongs in the Price column
 * beside it -- printing both here left Price and Value empty and made a property
 * the one row whose columns did not match their headers.
 */
function quantityCell(row) {
  if (row.is_house) return area(row.area_sqm);
  if (row.quantity == null) return "—";
  return Number(row.quantity).toLocaleString("en-US", { maximumFractionDigits: 6 });
}

/** What one unit cost: a square meter for a property, one share/gram otherwise. */
function priceCell(row) {
  if (row.is_house) {
    return (
      <span className="whitespace-nowrap">
        {row.quantity == null ? "—" : perSqm(Number(row.quantity) * 1e6)}
      </span>
    );
  }
  return unitPrice(row.unit_price_tomans, row.unit_price_currency);
}

/**
 * Correct one saved row.
 *
 * The history table used to render every quantity as a live input with a Save
 * button beside it, which made saved rows look like unsaved drafts — the single
 * loudest complaint about this page. Editing is now something you ask for.
 */
const CASH_KINDS = ["opening_cash", "deposit", "withdrawal", "dividend", "fee"];

function EditEntryDialog({ row, accounts, onClose, onSaved }) {
  const [kind, setKind] = useState(row.kind);
  const [targetAccount, setTargetAccount] = useState(String(row.account_id));
  const [when, setWhen] = useState(row.occurred_at || "");
  const [price, setPrice] = useState(row.unit_price_tomans ?? "");
  const [costBasis, setCostBasis] = useState(row.cost_basis_tomans ?? "");
  const [amount, setAmount] = useState(row.amount_tomans ?? "");
  const cash = CASH_KINDS.includes(kind);
  const tradeKind = kind === "buy" || kind === "sell";
  const declaresBasis = kind === "opening_position" || kind === "valuation_mark";
  const kindOptions = row.is_house ? ["opening_position", "valuation_mark"] :
    // A dividend is the one cash kind tied to an asset; the server refuses to
    // turn it into a plain deposit or the reverse.
    row.kind === "dividend" ? ["dividend"] :
    CASH_KINDS.includes(row.kind) ? ["opening_cash", "deposit", "withdrawal", "fee"] :
      ["opening_position", "buy", "sell", ...(row.kind === "rights_issue" ? ["rights_issue"] : [])];
  const [quantity, setQuantity] = useState(String(Number(row.quantity ?? 0)));
  // A property is described by two numbers and this dialog only ever offered
  // one, so its size was the one thing about it nobody could correct.
  const [areaSqm, setAreaSqm] = useState(
    row.area_sqm != null ? String(row.area_sqm) : ""
  );
  const [note, setNote] = useState(row.note || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // A row with no ledger history is disposed of by setting it to nothing, which
  // is the one place the server takes a zero; a real entry, and a property's
  // price per square meter, must be positive. This dialog previously asked only
  // that the box not be blank, so "0", "-2" and seven decimal places all reached
  // the server and came back as a failed save with the reason in a red banner.
  // A property's `quantity` is a price per square meter, which divides, so the
  // whole-unit rule is asked of the asset and never of the house.
  const qtyOpts = {
    allowZero: Boolean(row.is_synthetic),
    step: row.is_house ? "any" : row.quantity_step,
  };
  const qtyMessage = quantityError(quantity, qtyOpts);

  // An opening's unit price is what it is worth now; a trade's is what was
  // paid. Switching between them carries the PAID price across, or saving
  // would book today's value as the purchase price (and debit that cash).
  const isTrade = (k) => k === "buy" || k === "sell";
  const changeKind = (next) => {
    if (isTrade(next) && !isTrade(kind)) setPrice(costBasis);
    if (!isTrade(next) && isTrade(kind) && costBasis === "") setCostBasis(price);
    setKind(next);
  };

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      if (row.is_synthetic) {
        await updateLedgerHolding(row.account_id, row.holding_id, quantity);
      } else {
        await updateLedgerEntry(row.account_id, row.id, {
          ...(!cash ? { quantity } : { amount_tomans: amount }),
          // Send only what changed: the date picker yields midnight, so echoing
          // an untouched date would move the trade's time and reorder its day.
          ...(kind !== row.kind ? { kind } : {}),
          ...(Number(targetAccount) !== row.account_id ? { target_account_id: Number(targetAccount) } : {}),
          ...(when && when !== row.occurred_at ? { occurred_at: when } : {}),
          ...(tradeKind ? { unit_price_tomans: price } : {}),
          ...(declaresBasis && costBasis !== "" ? { cost_basis_tomans: costBasis } : {}),
          note,
          ...(row.is_house && areaSqm.trim() ? { area_sqm: areaSqm } : {}),
        });
      }
      await onSaved();
      onClose();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Edit ${isolate(holdingLabel(row))}`}
      subtitle={`${jalaliDate(row.occurred_at)} · ${dateTime(row.occurred_at)}`}
      onClose={onClose}
      testId="ledger-edit-dialog"
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button
            variant="primary"
            disabled={busy || (cash ? !(Number(amount) > 0) : !validQuantity(quantity, qtyOpts)) || (tradeKind && !(Number(price) > 0))}
            onClick={save}
            data-testid="ledger-edit-save"
          >
            {busy ? "Saving…" : "Save changes"}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <p role="alert" className="text-sm text-[var(--c-critical-text)]">{error}</p>}
        {!row.is_synthetic && <>
          <Field label="Portfolio"><Select label="Portfolio" value={targetAccount} onChange={(e) => setTargetAccount(e.target.value)} data-testid="ledger-edit-account" className="w-full">
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </Select></Field>
          <Field label="Type"><Select label="Type" value={kind} onChange={(e) => changeKind(e.target.value)} data-testid="ledger-edit-kind" className="w-full">
            {kindOptions.map((k) => <option key={k} value={k}>{KIND_LABEL[k] || humanize(k)}</option>)}
          </Select></Field>
          {/* Clearing the picker reads "Today" but the save sends no date, so the
              entry would keep its old one: clearing restores the saved date. */}
          <Field label="When"><JalaliDateField value={when} onChange={(v) => setWhen(v || row.occurred_at || "")} testId="ledger-edit-when" /></Field>
          {cash && <Field label="Amount (Toman)"><Input label="Amount" type="number" value={amount} onChange={(e) => setAmount(e.target.value)} data-testid="ledger-edit-amount" /></Field>}
          {tradeKind && <Field label={`Price for one (${row.unit_price_currency === "rial" ? "Rial" : row.unit_price_currency === "usd" ? "USD" : "Toman"})`}>
            <Input label="Unit price" type="number" step="any" value={price} onChange={(e) => setPrice(e.target.value)} data-testid="ledger-edit-price" />
          </Field>}
          {declaresBasis && <Field label={row.is_house ? "What did you pay per square meter? (millions of Toman)" : `What did you pay for one? (${row.unit_price_currency === "rial" ? "Rial" : "Toman"})`}>
            <Input label="Purchase price" type="number" step="any" value={costBasis} onChange={(e) => setCostBasis(e.target.value)} data-testid="ledger-edit-cost-basis" />
          </Field>}
        </>}
        {!cash && <>
        <div>
          <div className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">
            {row.is_house ? "Price per square meter (millions of Toman)" : "How many"}
          </div>
          <Input
            label="Quantity"
            type="number"
            step="any"
            className="w-full"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            data-testid="ledger-edit-qty"
          />
          {qtyMessage && (
            <span className="mt-1 block text-xs text-critical" data-testid="ledger-edit-qty-error">
              {qtyMessage}
            </span>
          )}
        </div>
        </>}
        {row.is_house && !row.is_synthetic && (
          <div>
            <div className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">
              Size (square meters)
            </div>
            <Input
              label="Size in square meters"
              type="number"
              step="any"
              min="0"
              className="w-full"
              value={areaSqm}
              onChange={(e) => setAreaSqm(e.target.value)}
              data-testid="ledger-edit-area"
            />
          </div>
        )}
        {!row.is_synthetic && (
          <div>
            <div className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">Note</div>
            <Input
              label="Note"
              className="w-full"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              data-testid="ledger-edit-note"
            />
          </div>
        )}
      </div>
    </Modal>
  );
}

export default function Ledger() {
  const { accounts, activeId, reload, loading: accountsLoading } = usePortfolio();
  const accountId = activeId ?? null;
  const ledger = useApi(() => listLedger(accountId), [accountId], {
    enabled: accounts.length > 0,
  });

  const [adding, setAdding] = useState(false);
  // The phone tab bar's "+" lands here with ?add=trade: open the dialog once,
  // then drop the flag so a refresh or back-navigation does not reopen it.
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    if (params.get("add") !== "trade") return;
    setAdding(true);
    const next = new URLSearchParams(params);
    next.delete("add");
    setParams(next, { replace: true });
  }, [params, setParams]);
  const [importing, setImporting] = useState(false);
  const [editing, setEditing] = useState(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = () => Promise.all([ledger.reload?.() ?? Promise.resolve(), reload()]);

  // Holding rows with their owning account attached: the dialog needs both to
  // revalue a property, and the account list is the only place they travel
  // together.
  const holdings = accounts.flatMap((a) =>
    (a.holdings || []).map((h) => ({ ...h, account_id: a.id }))
  );

  const [removing, setRemoving] = useState(null);
  const [undo, setUndo] = useState(null);
  // The confirm dialog covers the page-level alert, so a refused removal
  // reports inside the dialog or the click looks like it did nothing.
  const [removeError, setRemoveError] = useState("");
  const undoTimer = useRef(null);
  useEffect(() => () => clearTimeout(undoTimer.current), []);

  const removeRow = async (row) => {
    if (busy) return;
    setBusy(true);
    setError("");
    setRemoveError("");
    try {
      let removed = row;
      if (row.is_synthetic) {
        removed = await deleteLedgerHolding(row.account_id, row.holding_id);
      } else {
        await deleteLedgerEntry(row.account_id, row.id);
      }
      setRemoving(null);
      setUndo(removed);
      clearTimeout(undoTimer.current);
      undoTimer.current = setTimeout(() => setUndo(null), 8000);
      await refresh();
    } catch (err) {
      setRemoveError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };
  const undoRemoval = async () => {
    if (busy || !undo) return;
    clearTimeout(undoTimer.current);
    setBusy(true);
    setError("");
    try {
      // A synthetic row's removal returns the event it was preserved as, so
      // both kinds restore through the same endpoint.
      await restoreLedgerEntry(undo.account_id, undo.id);
      setUndo(null);
      await refresh();
    } catch (err) {
      // Drop the toast either way: it disables every Remove button, and a
      // refused restore (a later sale now depends on the shares) will not
      // succeed on a second click.
      setUndo(null);
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  // Accounts start as [] while the list is still in flight (PortfolioContext),
  // so this must wait for `loading` to clear before deciding there really is
  // no portfolio -- otherwise every navigation here flashes the wrong empty
  // state for however long the account list takes to arrive.
  if (accountsLoading && !accounts.length) {
    return <Loading testId="ledger-loading" />;
  }
  if (!accounts.length) {
    return <Empty testId="ledger-empty">Create a portfolio first.</Empty>;
  }

  // Only worth a column when there is more than one portfolio to tell apart.
  const showPortfolio = accountId == null && accounts.length > 1;

  const columns = [
    {
      key: "when",
      header: "When",
      mobile: "meta",
      // Dated on a Persian calendar when it was recorded, so read back on one.
      // The Gregorian date and the time of day stay on the hover.
      render: (r) => (
        <span className="whitespace-nowrap" title={dateTime(r.occurred_at)}>
          {jalaliDate(r.occurred_at)}
        </span>
      ),
    },
    { key: "kind", header: "What", mobile: "meta", render: kindBadge },
    {
      key: "asset",
      header: "Asset",
      mobile: "title",
      // The label is the ticker a share is recognized by, not the company's full
      // name; the full name stays reachable on hover.
      render: (r) =>
        r.asset_key ? (
          <bdi title={r.asset_name_fa || r.asset_name || ""}>{holdingLabel(r)}</bdi>
        ) : CASH_KINDS.includes(r.kind) ? (
          "Cash"
        ) : (
          "—"
        ),
    },
    { key: "qty", header: "Amount", align: "right", mobile: "meta", render: quantityCell },
    { key: "price", header: "Price", align: "right", render: priceCell },
    { key: "amt", header: "Value", align: "right", mobile: "value", render: (r) => toman(r.value_tomans) },
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
          {(r.quantity != null || CASH_KINDS.includes(r.kind)) && (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => setEditing(r)}
              data-testid="ledger-edit"
            >
              Edit
            </Button>
          )}
          <Button variant="danger" disabled={busy || !!undo} onClick={() => setRemoving(r)} data-testid="ledger-remove">Remove</Button>
        </div>
      ),
    },
  ];

  if (showPortfolio) {
    columns.splice(2, 0, {
      key: "portfolio",
      header: "Portfolio",
      mobile: "meta",
      render: (r) => r.account_name || "—",
    });
  }

  return (
    <div>
      <PageHeader
        title="Ledger"
        subtitle="Everything you have bought, sold, or already owned. Anything you hold without a recorded purchase is listed too, so you can fill in its details."
        actions={
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="primary"
              onClick={() => { setAdding(true); setError(""); }}
              data-testid="ledger-add"
              className="inline-flex items-center gap-1.5"
              aria-label="Add transaction"
            >
              <PlusIcon />
              Add
            </Button>
            {accountId != null && (
              <Button
                type="button"
                variant="secondary"
                onClick={() => { setImporting(true); setError(""); }}
                data-testid="ledger-import"
              >
                Import CSV
              </Button>
            )}
          </div>
        }
      />

      {error && (
        <p role="alert" className="mb-3 text-sm text-[var(--c-critical-text)]">{error}</p>
      )}

      {removing && <Modal title="Remove transaction?" onClose={() => { if (!busy) { setRemoving(null); setRemoveError(""); } }} testId="ledger-remove-dialog" footer={<>
        <Button disabled={busy} onClick={() => { setRemoving(null); setRemoveError(""); }} data-testid="ledger-remove-cancel">Cancel</Button>
        <Button variant="danger" disabled={busy} onClick={() => removeRow(removing)} data-testid="ledger-remove-confirm">{busy ? "Removing…" : "Remove"}</Button>
      </>}>
        {removeError && <p role="alert" className="mb-2 text-sm text-[var(--c-critical-text)]" data-testid="ledger-remove-error">{removeError}</p>}
        <p>{jalaliDate(removing.occurred_at)} · {holdingLabel(removing)} · {KIND_LABEL[removing.kind] || humanize(removing.kind)}</p>
        <p>{removing.account_name} · {quantityCell(removing)} · {toman(removing.value_tomans ?? removing.amount_tomans)}</p>
        <p className="mt-3 text-sm text-muted">Holdings and cash will update. {removing.is_synthetic ? "If this can be undone, an Undo button appears for 8 seconds." : "You can undo for 8 seconds. Your financial history is retained."}</p>
      </Modal>}
      {undo && <div role="status" className="fixed bottom-24 inset-x-4 z-40 mx-auto flex max-w-md items-center justify-between gap-3 rounded-lg border border-border bg-panel p-3 shadow-xl" data-testid="ledger-undo-toast">
        <span>Transaction removed.</span><Button disabled={busy} onClick={undoRemoval} data-testid="ledger-undo">Undo</Button>
      </div>}
      {adding && (
        <AddTransactionDialog
          accountId={accountId}
          accounts={accounts}
          holdings={holdings}
          onClose={() => setAdding(false)}
          onSaved={refresh}
        />
      )}
      {importing && (
        <ImportCsvDialog
          accountId={accountId}
          onClose={() => setImporting(false)}
          onImported={refresh}
        />
      )}

      {editing && (
        <EditEntryDialog
          row={editing}
          accounts={accounts}
          onClose={() => setEditing(null)}
          onSaved={refresh}
        />
      )}

      <Card
        title="History"
        testId="ledger-history-card"
        actions={
          <label className="flex items-center gap-2 text-sm text-muted">
            Rows
            <Select
              label="Rows per page"
              value={pageSize}
              onChange={(e) => { setPageSize(e.target.value); setPage(1); }}
              data-testid="ledger-page-size"
            >
              {PAGE_SIZES.map((s) => (
                <option key={s} value={s}>{s === ALL ? "All" : s}</option>
              ))}
            </Select>
          </label>
        }
      >
        {/* A page of entries is taller than a spinner, so without a reserved
            height the footer sits under the spinner and jumps a screenful when
            the rows arrive. */}
        <Async
          {...ledger}
          testId="ledger-history"
          empty="Nothing recorded yet. Use Add to record your first buy, sale, or holding."
          minHeight="60vh"
        >
          {(rows) => {
            // Clamped rather than corrected in state: deleting the last row of
            // the last page, or switching to a shorter portfolio, would
            // otherwise leave the table showing an empty page.
            const size = pageSize === ALL ? rows.length || 1 : Number(pageSize);
            const pages = Math.max(1, Math.ceil(rows.length / size));
            const current = Math.min(page, pages);
            return (
              <>
                <Table
                  testId="ledger-table"
                  caption="Transaction history"
                  mobileCards
                  rowKey={(r) => r.id}
                  rows={rows.slice((current - 1) * size, current * size)}
                  columns={columns}
                />
                {pages > 1 && (
                  <Pager
                    page={current}
                    count={rows.length}
                    pageSize={size}
                    onPage={setPage}
                    testId="ledger-pager"
                  />
                )}
              </>
            );
          }}
        </Async>
      </Card>
    </div>
  );
}

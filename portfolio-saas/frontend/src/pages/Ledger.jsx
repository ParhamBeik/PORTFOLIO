import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  commitLedgerImport,
  deleteLedgerEntry,
  deleteLedgerHolding,
  listLedger,
  reverseLedgerEntry,
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
function EditEntryDialog({ row, onClose, onSaved }) {
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

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      if (row.is_synthetic) {
        await updateLedgerHolding(row.account_id, row.holding_id, quantity);
      } else {
        await updateLedgerEntry(row.account_id, row.id, {
          quantity,
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
            disabled={busy || !validQuantity(quantity, qtyOpts)}
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

  const reverseRow = async (row) => {
    if (busy || row.is_synthetic) return;
    if (!window.confirm(
      `Reverse this ${(KIND_LABEL[row.kind] || row.kind).toLowerCase()} entry? Holdings update, and this row leaves the History list. The correction stays on the books.`
    )) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      await reverseLedgerEntry(row.account_id, row.id);
      await refresh();
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const removeRow = async (row) => {
    if (busy) return;
    if (!window.confirm(`Delete this ${(KIND_LABEL[row.kind] || row.kind).toLowerCase()} entry?`)) {
      return;
    }
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

  const showPortfolio = accountId == null;

  const columns = [
    {
      key: "when",
      header: "When",
      // Dated on a Persian calendar when it was recorded, so read back on one.
      // The Gregorian date and the time of day stay on the hover.
      render: (r) => (
        <span className="whitespace-nowrap" title={dateTime(r.occurred_at)}>
          {jalaliDate(r.occurred_at)}
        </span>
      ),
    },
    { key: "kind", header: "What", render: kindBadge },
    {
      key: "asset",
      header: "Asset",
      // The label is the ticker a share is recognized by, not the company's full
      // name; the full name stays reachable on hover.
      render: (r) =>
        r.asset_key ? (
          <bdi title={r.asset_name_fa || r.asset_name || ""}>{holdingLabel(r)}</bdi>
        ) : (
          "—"
        ),
    },
    { key: "qty", header: "Amount", align: "right", render: quantityCell },
    { key: "price", header: "Price", align: "right", render: priceCell },
    { key: "amt", header: "Value", align: "right", render: (r) => toman(r.value_tomans) },
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
              disabled={busy}
              onClick={() => setEditing(r)}
              data-testid="ledger-edit"
            >
              Edit
            </Button>
          )}
          {!r.is_synthetic && (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => reverseRow(r)}
              data-testid="ledger-reverse"
            >
              Reverse
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

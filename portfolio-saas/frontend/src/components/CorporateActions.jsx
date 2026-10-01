import { useState } from "react";
import { useApi } from "../useApi.js";
import {
  acceptCorporateAction,
  corporateActionSuggestions,
  dismissCorporateAction,
} from "../api.js";
import { num } from "../format.js";
import { Button, Card, ErrorState, Table } from "./ui.jsx";

/**
 * Capital increases on the user's TSE holdings, waiting for a yes or no.
 *
 * Nothing is booked until the user accepts: the detected factor can carry a
 * same-day dividend, and a paid rights issue is not free shares, so the
 * suggested quantity is editable. Renders nothing when there is nothing to
 * decide, so the home page stays quiet.
 */
export default function CorporateActionsCard({ onBooked }) {
  const state = useApi(corporateActionSuggestions, []);
  const [drafts, setDrafts] = useState({});
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const rows = state.data?.results || [];
  if (!rows.length) return null;

  const keyOf = (r) => `${r.account_id}:${r.symbol}:${r.date}`;
  const decide = async (row, accept) => {
    setBusy(keyOf(row));
    setError(null);
    try {
      const target = { symbol: row.symbol, date: row.date };
      if (accept) {
        await acceptCorporateAction(row.account_id, {
          ...target, quantity: drafts[keyOf(row)] ?? row.suggested_quantity,
        });
        onBooked?.();
      } else {
        await dismissCorporateAction(row.account_id, target);
      }
      state.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const columns = [
    { key: "symbol", header: "Stock", render: (r) => <bdi>{r.symbol}</bdi> },
    { key: "account_name", header: "Portfolio" },
    { key: "date", header: "Date", render: (r) => <bdi>{r.date}</bdi> },
    { key: "held", header: "Held before", align: "right", render: (r) => num(Number(r.held_quantity)) },
    {
      key: "added",
      header: "New shares",
      align: "right",
      render: (r) => (
        <input
          className="w-28 rounded border border-border bg-transparent px-2 py-1 text-right"
          inputMode="numeric"
          aria-label={`New shares of ${r.symbol} on ${r.date}`}
          value={drafts[keyOf(r)] ?? r.suggested_quantity}
          onChange={(e) => setDrafts((d) => ({ ...d, [keyOf(r)]: e.target.value }))}
          data-testid="dashboard-corporate-actions-quantity"
        />
      ),
    },
    {
      key: "decide",
      header: "",
      render: (r) => (
        <div className="flex justify-end gap-2">
          <Button variant="primary" disabled={busy === keyOf(r)} onClick={() => decide(r, true)}
            data-testid="dashboard-corporate-actions-accept">Add shares</Button>
          <Button disabled={busy === keyOf(r)} onClick={() => decide(r, false)}
            data-testid="dashboard-corporate-actions-dismiss">Not mine</Button>
        </div>
      ),
    },
  ];

  return (
    <Card
      title="Capital increases to confirm"
      subtitle="Detected from exchange prices and Codal. Check the share count against your broker before adding."
      testId="dashboard-corporate-actions"
    >
      {error && <ErrorState error={error} testId="dashboard-corporate-actions-error" />}
      <Table columns={columns} rows={rows} rowKey={keyOf} testId="dashboard-corporate-actions-table" mobileCards />
    </Card>
  );
}

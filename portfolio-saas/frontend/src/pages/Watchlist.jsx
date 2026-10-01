import { useState } from "react";
import { Link } from "react-router-dom";
import { unwatch, watchlist } from "../api.js";
import { Async, Button, Card, Table } from "../components/ui.jsx";
import { rial } from "../format.js";
import { useApi } from "../useApi.js";

/**
 * Companies followed without owning them. Each row opens the same research
 * view a holding does; filings on these will raise the same notices.
 */
export default function Watchlist() {
  const state = useApi(() => watchlist(), []);
  const [busy, setBusy] = useState(null);

  const remove = async (symbol) => {
    setBusy(symbol);
    try {
      await unwatch(symbol);
      state.reload();
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card
      title="Watchlist"
      subtitle="Companies you follow without holding them. Add one from its research page."
      testId="watchlist"
    >
      <Async {...state} testId="watchlist-body">
        {(data) => (
          <Table
            testId="watchlist-table"
            mobileCards
            rowKey={(r) => r.symbol}
            rows={data.results}
            empty="Nothing followed yet. Open a company under Companies and choose “Watch”."
            columns={[
              {
                key: "symbol",
                header: "Company",
                render: (r) => (
                  <Link
                    to={`/research?${new URLSearchParams({ view: "companies", symbol: r.symbol })}`}
                    className="text-accent underline"
                  >
                    <bdi>{r.symbol}</bdi>
                  </Link>
                ),
              },
              {
                key: "last",
                header: "Last close",
                align: "right",
                render: (r) => (r.last_close_rial ? `${rial(r.last_close_rial)} · ${r.last_close_date}` : "—"),
              },
              {
                key: "remove",
                header: "",
                align: "right",
                render: (r) => (
                  <Button
                    variant="ghost"
                    disabled={busy === r.symbol}
                    onClick={() => remove(r.symbol)}
                    data-testid={`watchlist-remove-${r.symbol}`}
                  >
                    Remove
                  </Button>
                ),
              },
            ]}
          />
        )}
      </Async>
    </Card>
  );
}

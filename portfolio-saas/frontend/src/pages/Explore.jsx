import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { exploreStocks, stockDossier } from "../api.js";
import { MultiLineTrend } from "../components/charts.jsx";
import { Async, Badge, Button, Card, Empty, Input, PageHeader, Tabs } from "../components/ui.jsx";
import { date, num, rial } from "../format.js";
import { useApi } from "../useApi.js";

const WINDOWS = [
  { value: "90", label: "90D" },
  { value: "365", label: "1Y" },
  { value: "1825", label: "5Y" },
  { value: "3650", label: "10Y" },
];

const COVERAGE_LABELS = {
  stock_history_unadjusted: "Daily stock closes",
  stock_candle_unadjusted: "Unadjusted candles",
  codal_announcements: "Codal disclosures",
};

function Company({ symbol }) {
  const [days, setDays] = useState("365");
  const dossier = useApi(() => stockDossier(symbol, Number(days)), [symbol, days]);

  return (
    <Async {...dossier} testId="explore-company-result" minHeight="320px">
      {(data) => {
        const price = data.price;
        const rows = price.points.map((point) => ({ x: point.date, close: Number(point.close_rial) }));
        return (
          <div className="space-y-5" data-testid="explore-company">
            <Card
              title={`${data.company.name || symbol} · ${symbol}`}
              subtitle={[data.company.sector, data.company.subsector, data.company.isin].filter(Boolean).join(" · ")}
              testId="explore-identity"
              actions={<Badge variant="warn">Financial metrics unverified</Badge>}
            >
              <p className="text-sm text-muted">
                Company classification comes from the TSE instrument catalog. The stored Codal figures have not
                passed unit, period, and source reconciliation, so they cannot yet support a profit or margin comparison.
              </p>
            </Card>

            <Card
              title="Daily share price"
              subtitle="Unadjusted last trade close in Rial per share. This is a quoted price, not a total return."
              testId="explore-price"
              actions={<Tabs options={WINDOWS} value={days} onChange={setDays} label="Price window" testId="explore-price-window" />}
            >
              {rows.length ? (
                <MultiLineTrend
                  series={[{ key: "close", name: `${symbol} (Rial)` }]}
                  data={rows}
                  longTicks={rows.length > 180}
                  formatValue={rial}
                  formatAxis={rial}
                  label={`${symbol} provider-reported daily closes in Rial`}
                />
              ) : <Empty testId="explore-no-prices">No daily close in this period.</Empty>}
              <div className="mt-3 space-y-1 text-xs text-muted" data-testid="explore-price-evidence">
                <p>Source: {price.source}. {price.points.length} dated closes; {price.first_date || "—"} → {price.last_date || "—"} (Jalali).</p>
                <p>
                  {price.paired_candle_days} days checked against the separate unadjusted candle feed;
                  {" "}{price.candle_disagreements_over_1pct} differ by more than 1%.
                  {price.candle_disagreements_over_1pct > 0 && " Treat this chart as disputed until those days are reconciled."}
                </p>
                <p>Adjusted prices, USD conversion, and return comparisons are withheld pending source checks.</p>
              </div>
            </Card>

            <div className="grid gap-5 lg:grid-cols-2">
              <Card title="Warehouse coverage" subtitle="Ingestion progress, not a guarantee that every value is correct." testId="explore-coverage">
                <ul className="space-y-3">
                  {data.coverage.map((row) => (
                    <li key={row.endpoint} className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2 text-sm">
                      <span>{COVERAGE_LABELS[row.endpoint] || row.endpoint}</span>
                      <span className="flex items-center gap-2">
                        <span className="text-muted">{num(row.stored_rows, 0)} rows</span>
                        <Badge variant={row.verified_complete ? "good" : "warn"}>
                          {row.verified_complete ? "Coverage checked" : "Coverage incomplete"}
                        </Badge>
                      </span>
                      {row.last_success_at && <span className="w-full text-xs text-muted">Last successful fetch {date(row.last_success_at)}</span>}
                    </li>
                  ))}
                </ul>
              </Card>
              <Card title="Original disclosures" subtitle="Source announcements only; no extracted figure is treated as verified." testId="explore-disclosures">
                {data.disclosures.length ? (
                  <ul className="space-y-3 text-sm">
                    {data.disclosures.map((item, index) => (
                      <li key={`${item.published_jalali}-${index}`} className="border-b border-border pb-2">
                        {item.source_url ? (
                          <a className="text-accent underline underline-offset-2" href={item.source_url} target="_blank" rel="noopener noreferrer">
                            {item.title}
                          </a>
                        ) : <span>{item.title}</span>}
                        <span className="mt-1 block text-xs text-muted">{item.published_jalali || "Date unavailable"} · {item.category || "Unclassified"} · Metrics unverified</span>
                      </li>
                    ))}
                  </ul>
                ) : <Empty testId="explore-no-disclosures">No stored disclosure for this symbol.</Empty>}
              </Card>
            </div>
            {data.company.metadata_updated_at && (
              <p className="text-xs text-muted">Company catalog last updated {date(data.company.metadata_updated_at)}.</p>
            )}
          </div>
        );
      }}
    </Async>
  );
}

export default function Explore() {
  const [params, setParams] = useSearchParams();
  const symbol = params.get("symbol") || "";
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const results = useApi(() => exploreStocks(query), [query]);

  return (
    <div className="space-y-5">
      <PageHeader title="Explore companies" subtitle="Find a TSE company and inspect prices, coverage, and the original disclosures behind future research." />
      <Card title="Find a company" testId="explore-search">
        <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); setQuery(draft.trim()); }}>
          <Input label="Search by TSE symbol or company name" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Symbol or company name" maxLength={100} className="min-w-52 flex-1" data-testid="explore-query" />
          <Button type="submit" variant="ghost" data-testid="explore-submit">Search</Button>
        </form>
        <Async {...results} testId="explore-results" empty="No stocks match this search.">
          {(stocks) => stocks.length ? (
            <div className="mt-3 flex max-h-48 flex-wrap gap-2 overflow-y-auto" data-testid="explore-stock-list">
              {stocks.map((stock) => (
                <Button
                  key={stock.symbol}
                  onClick={() => setParams({ symbol: stock.symbol })}
                  aria-pressed={symbol === stock.symbol}
                  className={symbol === stock.symbol ? "border-accent text-accent" : ""}
                  data-testid={`explore-stock-${stock.symbol}`}
                >
                  {stock.symbol} · {stock.name}
                </Button>
              ))}
            </div>
          ) : <Empty testId="explore-no-results">No eligible TSE stock matches.</Empty>}
        </Async>
      </Card>
      {symbol ? <Company key={symbol} symbol={symbol} /> : (
        <Empty testId="explore-prompt">Choose a company to inspect its source data.</Empty>
      )}
    </div>
  );
}

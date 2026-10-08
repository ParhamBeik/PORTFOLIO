import { useEffect, useState } from "react";
import { translate, useLang } from "../i18n.js";
import { Link, useSearchParams } from "react-router-dom";
import { downloadArchivedFiling, exploreStocks, researchRun, researchSettings, runResearch, stockDossier, watch } from "../api.js";
import { MultiLineTrend } from "../components/charts.jsx";
import { Async, Badge, Button, Card, Empty, Field, Input, PageHeader, Tabs, Textarea } from "../components/ui.jsx";
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

const RESEARCH_GAPS = {
  no_verified_financial_evidence: "No monthly sales, income statement, or balance sheet for this company has passed source reconciliation in the available window.",
  question_needs_uncertified_data: "This question needs financial, USD, industry, or other evidence that has not passed validation.",
  unsupported_by_verified_tools: "The verified sales and statement observations cannot answer that question yet.",
};

function reportedAmount(value) {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(value));
  if (!match) return "—";
  const fraction = (match[3] || "").replace(/0+$/, "");
  return `${match[1]}${match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction ? `.${fraction}` : ""}`;
}

function ArchivedFiling({ symbol, point, days = 365 }) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  if (!point.extraction_id) return null;
  async function download() {
    setWorking(true);
    setError("");
    try {
      await downloadArchivedFiling(symbol, point.extraction_id, days);
    } catch (caught) {
      setError(caught.message || "Archived filing unavailable.");
    } finally {
      setWorking(false);
    }
  }
  return <>
    <Button type="button" variant="ghost" onClick={download} disabled={working} className="text-xs" data-testid={`explore-archive-${point.extraction_id}`}>
      {working ? "Checking archive…" : "Download archived filing"}
    </Button>
    {point.artifact_sha256 && <span className="block break-all text-xs text-muted">Archived SHA-256: {point.artifact_sha256}</span>}
    {error && <span role="alert" className="block text-xs text-[var(--c-critical-text)]">{error}</span>}
  </>;
}

function ResearchClaims({ claims, symbol }) {
  return claims.map((claim) => (
    <div key={claim.id} className="text-sm">
      <p>{claim.statement}</p>
      <ul className="mt-1 space-y-1 text-xs text-muted">
        {claim.sources.map((source) => (
          <li key={`${claim.id}-${source.extraction_id}`}>
            {source.period_end_jalali} · {source.scope
              ? source.statement_kind === "balance_sheet"
                ? `${source.scope} balance cells assets ${source.source_coordinates.total_assets.address} / liabilities ${source.source_coordinates.total_liabilities.address} / equity ${source.source_coordinates.total_equity.address}`
                : `${source.scope} income cells ${source.source_coordinates.revenue.address} / ${source.source_coordinates.net_profit.address}`
              : `sales row ${source.source_coordinates.row ?? "?"}, column ${source.source_coordinates.column ?? "?"}`} · artifact {source.artifact_id}
            {source.source_url && <> · <a href={source.source_url} target="_blank" rel="noopener noreferrer" className="text-accent underline">Codal filing</a></>}
            {" "}<ArchivedFiling symbol={symbol} point={source} days={source.scope ? 3650 : 365} />
          </li>
        ))}
      </ul>
    </div>
  ));
}

function SavedResearch({ symbol, runId }) {
  const saved = useApi(() => researchRun(runId), [runId]);
  return <Async {...saved} testId="explore-saved-research">
    {(run) => run.symbol === symbol ? (
      <div className="space-y-3 border-t border-border pt-3">
        <p className="text-sm font-medium">Saved answer · {date(run.created_at)}</p>
        <p className="text-sm text-muted">{run.question}</p>
        {run.status === "answered" && (
          <p role="status" className={`text-sm ${run.evidence_state === "current" ? "text-muted" : "text-[var(--c-critical-text)]"}`}>
            {run.evidence_state === "current"
              ? "Source selection and calculation still match the current warehouse. The archived file is checked when downloaded."
              : "Historical answer: its source selection is changed or cannot be rechecked. Do not use it as a current figure."}
          </p>
        )}
        {run.claims?.length > 0 && <ResearchClaims claims={run.claims} symbol={symbol} />}
        {run.status !== "answered" && <p className="text-sm">No answer was issued: {RESEARCH_GAPS[run.failure_code] || run.failure_code || run.status}.</p>}
        <p className="text-xs text-muted">{run.cost_usd == null
          ? `Model cost unknown; $${run.reserved_usd} remains reserved (${run.cost_basis}).`
          : `Recorded model cost: $${num(Number(run.cost_usd), 6)} (${run.cost_basis}).`}</p>
      </div>
    ) : <p role="alert" className="text-sm">This saved answer belongs to {run.symbol}, not {symbol}.</p>}
  </Async>;
}

function ResearchPanel({ symbol }) {
  const [params, setParams] = useSearchParams();
  const runId = params.get("run");
  const settings = useApi(() => researchSettings(), []);
  const [question, setQuestion] = useState("");
  const [budget, setBudget] = useState("0.01");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

  useEffect(() => {
    if (settings.data) setBudget(settings.data.default_run_usd);
  }, [settings.data]);

  async function submit(event) {
    event.preventDefault();
    if (runId) setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.delete("run");
      return next;
    });
    setWorking(true);
    setError("");
    setResult(null);
    try {
      setResult(await runResearch(symbol, question.trim(), budget));
    } catch (caught) {
      setError(caught.message || "Research could not complete.");
    } finally {
      setWorking(false);
    }
  }

  return (
    <Card
      title="Ask about verified sales and statements"
      subtitle="One bounded GapGPT call selects from source-backed calculations. USD, peer, and valuation questions still abstain."
      testId="explore-research"
    >
      <Async {...settings} testId="explore-research-settings">
        {(config) => (
          <div className="space-y-3">
            <p className="text-xs text-muted">
              {config.provider_status === "ready"
                ? `Model: ${config.provider_model}. Your question goes to GapGPT; portfolio holdings do not. Choose a per-run cost ceiling; the server also enforces a daily research ceiling.`
                : "GapGPT is not yet connected to the News project's server-side settings. Verified charts above remain available."}
            </p>
            <form className="space-y-3" onSubmit={submit}>
              <Field label="Your question" hint="For example: Which verified month had the highest sales?">
                <Textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={3} minLength={3} maxLength={600} required className="w-full" label="Sales research question" data-testid="explore-research-question" />
              </Field>
              <div className="flex flex-wrap items-end gap-3">
                <Field label="Maximum model cost (USD)" hint={`Server limit $${config.max_run_usd} per run`}>
                  <Input type="number" min="0.000001" max={config.max_run_usd} step="0.000001" value={budget} onChange={(event) => setBudget(event.target.value)} required label="Maximum model cost in USD" data-testid="explore-research-budget" />
                </Field>
                <Button type="submit" variant="primary" disabled={working || config.provider_status !== "ready"} data-testid="explore-research-submit">
                  {working ? "Checking evidence…" : "Run research"}
                </Button>
              </div>
            </form>
            {error && <p role="alert" className="text-sm text-[var(--c-critical-text)]">{error}</p>}
            {runId && (/^[1-9]\d*$/.test(runId)
              ? <SavedResearch symbol={symbol} runId={runId} />
              : <p role="alert" className="text-sm">Invalid saved answer link.</p>)}
            {result && (
              <div className="space-y-3 border-t border-border pt-3" data-testid="explore-research-result">
                {result.claims?.length ? <ResearchClaims claims={result.claims} symbol={symbol} /> : <p className="text-sm">{RESEARCH_GAPS[result.reason] || "The verified evidence cannot answer this question."}</p>}
                {result.run_id && <Link to={`/research?${new URLSearchParams({ view: "companies", symbol, run: String(result.run_id) })}`} onClick={() => setResult(null)} className="text-sm text-accent underline">Open saved answer with a fresh source check</Link>}
                {result.coverage && <p className="text-xs text-muted">Coverage: sales {result.coverage.verified_periods} verified / {result.coverage.withheld_periods} withheld; income {result.coverage.income_verified_periods} verified / {result.coverage.income_withheld_periods} withheld; balance {result.coverage.balance_verified_periods} verified / {result.coverage.balance_withheld_periods} withheld latest filings.</p>}
                <p className="text-xs text-muted">Model cost: ${num(Number(result.cost_usd), 6)} ({result.cost_basis}). Numeric claims come from stored calculations; the model selected which ones address your question.</p>
              </div>
            )}
          </div>
        )}
      </Async>
    </Card>
  );
}

/** Follow this company from its research page; the list lives under Research → Watchlist. */
function WatchButton({ symbol }) {
  const [state, setState] = useState("idle");
  const add = async () => {
    setState("busy");
    try {
      await watch(symbol);
      setState("done");
    } catch {
      setState("error");
    }
  };
  if (state === "done") return <span className="text-sm text-muted" data-testid="explore-watched">On your watchlist</span>;
  return (
    <Button variant="ghost" onClick={add} disabled={state === "busy"} data-testid="explore-watch">
      {state === "error" ? "Could not add — retry" : "Watch"}
    </Button>
  );
}

function Company({ symbol }) {
  const lang = useLang();
  const [days, setDays] = useState("365");
  const dossier = useApi(() => stockDossier(symbol, Number(days)), [symbol, days]);

  return (
    <Async {...dossier} testId="explore-company-result" minHeight="320px">
      {(data) => {
        const price = data.price;
        const rows = price.points.map((point) => ({ x: point.date, close: Number(point.close_rial) }));
        const sales = data.monthly_sales;
        const salesRows = sales.points.map((point) => ({ x: point.date, sales: Number(point.value) }));
        const income = data.financial_metrics;
        const balance = data.balance_sheet;
        return (
          <div className="space-y-5" data-testid="explore-company">
            <Card
              title={`${data.company.name || symbol} · ${symbol}`}
              subtitle={[data.company.sector, data.company.subsector, data.company.isin].filter(Boolean).join(" · ")}
              testId="explore-identity"
              actions={<div className="flex flex-wrap items-center gap-2"><WatchButton symbol={symbol} /><Badge variant={sales.points.length || income?.points?.length || balance?.points?.length ? "good" : "warn"}>{sales.points.length || income?.points?.length || balance?.points?.length ? "Source-checked figures" : "Financial metrics unverified"}</Badge></div>}
            >
              <p className="text-sm text-muted">
                {data.company.sector ? (
                  <>Industry is a current provider-reported {data.company.sector_source === "symbol_metadata" ? "symbol metadata" : "instrument catalog"} label, observed {date(data.company.sector_observed_at)}; it does not establish past membership. </>
                ) : "Industry is unavailable for this stock. "}
                {translate("Monthly sales appear only where the current Codal filing reconciles to its source rows. Income and balance figures appear only for supported statement templates with verified issuer, unit, period, and arithmetic.", lang)}
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
                <p>{translate("Adjusted prices, USD conversion, and return comparisons are withheld pending source checks.", lang)}</p>
              </div>
            </Card>

            <Card
              title="Monthly sales"
              subtitle="Current-month reported sales in million Rial. A later unverified correction withholds the older value."
              testId="explore-monthly-sales"
              actions={<Badge variant={sales.withheld_periods ? "warn" : sales.points.length ? "good" : "warn"}>{sales.verified_periods} checked · {sales.withheld_periods} withheld</Badge>}
            >
              {salesRows.length ? (
                <MultiLineTrend
                  series={[{ key: "sales", name: `${symbol} (million Rial)` }]}
                  data={salesRows}
                  formatValue={(value) => `${num(value, 0)} million Rial`}
                  formatAxis={(value) => num(value, 0)}
                  label={`${symbol} source-reconciled monthly sales in million Rial`}
                />
              ) : <Empty testId="explore-no-monthly-sales">No monthly sales total has passed source reconciliation in this window.</Empty>}
              <p className="mt-3 text-xs text-muted">Verified means source-row arithmetic, period, denomination, and archived artifact checksum were checked. It is not an audit of the company’s accounts.</p>
              {sales.points.length > 0 && (
                <details className="mt-3 text-sm" data-testid="explore-sales-evidence">
                  <summary className="cursor-pointer">Inspect source cells</summary>
                  <ul className="mt-2 max-h-56 space-y-2 overflow-y-auto">
                    {sales.points.slice().reverse().map((point) => (
                      <li key={`${point.period_end_jalali}-${point.extraction_id}`} className="border-b border-border pb-2">
                        <span>{point.period_end_jalali}: {num(Number(point.value), 0)} million Rial</span>
                        <span className="block text-xs text-muted">Filed {point.published_jalali || "date unavailable"}{point.is_correction ? " · correction" : ""} · report {point.report_id} · {point.source_coordinates.css || point.source_coordinates.sheet || "table"}, row {point.source_coordinates.row ?? "?"}, column {point.source_coordinates.column ?? "?"}</span>
                        {point.source_url && <a className="text-accent underline underline-offset-2" href={point.source_url} target="_blank" rel="noopener noreferrer">Original Codal filing</a>}
                        {" "}<ArchivedFiling symbol={symbol} point={point} days={Number(days)} />
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </Card>
            <Card
              title="Revenue and net profit"
              subtitle="Reported income statement figures in million Rial. Standalone and consolidated filings stay separate; a newer unverified filing withholds an older figure."
              testId="explore-income"
              actions={<Badge variant={income?.withheld_periods ? "warn" : income?.points?.length ? "good" : "warn"}>{income?.verified_periods || 0} checked · {income?.withheld_periods || 0} withheld</Badge>}
            >
              {income?.points?.length ? (
                <div className="max-h-96 space-y-3 overflow-y-auto" data-testid="explore-income-evidence">
                  {income.points.slice().reverse().map((point) => (
                    <div key={`${point.period_end_jalali}-${point.scope}`} className="border-b border-border pb-3 text-sm">
                      <p className="font-medium">{point.period_start_jalali} → {point.period_end_jalali} · {point.scope} · {point.audited ? "audited" : "unaudited"}</p>
                      <p>Revenue {reportedAmount(point.revenue)} · net profit {reportedAmount(point.net_profit)} million Rial · net margin {point.net_margin_pct}%</p>
                      <p className="text-xs text-muted">Filed {point.published_jalali || "date unavailable"}{point.is_correction ? " · correction" : ""} · artifact {point.artifact_id} · revenue cell {point.source_coordinates.revenue.address} · profit cell {point.source_coordinates.net_profit.address}</p>
                      {point.source_url && <a className="text-xs text-accent underline underline-offset-2" href={point.source_url} target="_blank" rel="noopener noreferrer">Original Codal filing</a>}
                      {" "}<ArchivedFiling symbol={symbol} point={point} days={Number(days)} />
                    </div>
                  ))}
                </div>
              ) : <Empty testId="explore-no-income">No income statement has passed source reconciliation in this window.</Empty>}
              <p className="mt-3 text-xs text-muted">The source cells and arithmetic were checked against the archived filing. This does not audit the company’s accounts or make interim and annual figures comparable.</p>
            </Card>
            <Card
              title="Assets, liabilities, and equity"
              subtitle="Point-in-time balance sheet amounts in million Rial. Standalone and consolidated filings stay separate; a newer unverified filing withholds an older balance."
              testId="explore-balance"
              actions={<Badge variant={balance?.withheld_periods ? "warn" : balance?.points?.length ? "good" : "warn"}>{balance?.verified_periods || 0} checked · {balance?.withheld_periods || 0} withheld</Badge>}
            >
              {balance?.points?.length ? (
                <div className="max-h-96 space-y-3 overflow-y-auto" data-testid="explore-balance-evidence">
                  {balance.points.slice().reverse().map((point) => (
                    <div key={`${point.period_end_jalali}-${point.scope}`} className="border-b border-border pb-3 text-sm">
                      <p className="font-medium">{point.period_end_jalali} · {point.scope} · {point.audited ? "audited" : "unaudited"}</p>
                      <p>Assets {reportedAmount(point.values.total_assets)} · liabilities {reportedAmount(point.values.total_liabilities)} · equity {reportedAmount(point.values.total_equity)} million Rial</p>
                      <p>Cash {reportedAmount(point.values.cash)} · short-term borrowings {reportedAmount(point.values.short_term_borrowings)} · long-term borrowings {reportedAmount(point.values.long_term_borrowings)} million Rial</p>
                      <p className="text-xs text-muted">Filed {point.published_jalali || "date unavailable"}{point.is_correction ? " · correction" : ""} · artifact {point.artifact_id} · assets cell {point.source_coordinates.total_assets.address} · liabilities cell {point.source_coordinates.total_liabilities.address} · equity cell {point.source_coordinates.total_equity.address}</p>
                      {point.source_url && <a className="text-xs text-accent underline underline-offset-2" href={point.source_url} target="_blank" rel="noopener noreferrer">Original Codal balance sheet</a>}
                      {" "}<ArchivedFiling symbol={symbol} point={point} days={Number(days)} />
                    </div>
                  ))}
                </div>
              ) : <Empty testId="explore-no-balance">No balance sheet has passed source reconciliation in this window.</Empty>}
              <p className="mt-3 text-xs text-muted">The archived cells satisfy assets = liabilities + equity and component totals. This does not audit the company’s accounts or imply a valuation.</p>
            </Card>
            <ResearchPanel symbol={symbol} />

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
              <Card title="Original disclosures" subtitle="Source announcements; reconciled sales, income, and balance figures appear above where available." testId="explore-disclosures">
                {data.disclosures.length ? (
                  <ul className="space-y-3 text-sm">
                    {data.disclosures.map((item, index) => (
                      <li key={`${item.published_jalali}-${index}`} className="border-b border-border pb-2">
                        {item.source_url ? (
                          <a className="text-accent underline underline-offset-2" href={item.source_url} target="_blank" rel="noopener noreferrer">
                            {item.title}
                          </a>
                        ) : <span>{item.title}</span>}
                        <span className="mt-1 block text-xs text-muted">{item.published_jalali || "Date unavailable"} · {item.category || "Unclassified"} ({item.category_basis === "category" ? "provider label" : item.category_basis === "title" ? "title-based label" : "unclassified"}) · Raw filing</span>
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

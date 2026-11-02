import { useEffect, useState } from "react";
import { translate, useLang } from "../i18n.js";
import { Link, useSearchParams } from "react-router-dom";
import { downloadArchivedFiling, exploreStocks, researchRun, researchSettings, runResearch, stockDossier, unwatch, watch, watchlist } from "../api.js";
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
  no_verified_financial_evidence: "We could not check any monthly sales, income statement or balance sheet for this company against its filings.",
  question_needs_uncertified_data: "Answering this needs figures we have not been able to check yet.",
  unsupported_by_verified_tools: "The verified sales and statement observations cannot answer that question yet.",
};

// The dossier's sentences sit in small helpers below the page's `useLang`; they
// re-render with it, so reading the current language here is enough.
const tr = (text, vars) => translate(text, undefined, vars);

// Answers are stored as English sentences built from fixed templates
// (backend/research/observations.py), so each template has a translatable form.
const CLAIMS = [
  [/^Highest among verified months: (.+) at ([\d,.]+) million Rial\.$/, "Highest among verified months: {a}, {b} million Rial."],
  [/^Lowest among verified months: (.+) at ([\d,.]+) million Rial\.$/, "Lowest among verified months: {a}, {b} million Rial."],
  [/^Latest verified month (.+): ([\d,.]+) million Rial\.$/, "Latest verified month {a}: {b} million Rial."],
  [/^(\d+) of (\d+) latest monthly filings have source-reconciled sales; (\d+) are withheld\.$/, "{a} of the latest {b} monthly filings have checked sales; {c} are withheld."],
  [/^Sales changed ([+\-\d,.]+)% from (.+) to (.+)\.$/, "Sales changed {a}% from {b} to {c}."],
  [/^Latest verified (\w+), (audited|unaudited) income statement for (.+): (operating revenue|net profit|net profit margin) (.+)\.$/, "Latest checked income statement ({a}, {b}) for {c}: {d} {e}.", [0, 1, 3]],
  [/^Latest verified (\w+), (audited|unaudited) balance sheet at (.+): ([a-z\- ]+) (.+)\.$/, "Latest checked balance sheet ({a}, {b}) at {c}: {d} {e}.", [0, 1, 3]],
];

function claimText(statement) {
  for (const [re, key, words = []] of CLAIMS) {
    const m = statement.match(re);
    if (!m) continue;
    const vars = {};
    m.slice(1).forEach((value, i) => {
      vars["abcde"[i]] = words.includes(i) ? tr(value) : value.replace(/ million Rial$/, ` ${tr("million Rial")}`);
    });
    return tr(key, vars);
  }
  return statement;
}

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
      setError(caught.message || tr("Archived filing unavailable."));
    } finally {
      setWorking(false);
    }
  }
  return <>
    <Button type="button" variant="ghost" onClick={download} disabled={working} className="text-xs" data-testid={`explore-archive-${point.extraction_id}`}>
      {working ? "Checking archive…" : "Download archived filing"}
    </Button>
    {error && <span role="alert" className="block text-xs text-[var(--c-critical-text)]">{error}</span>}
  </>;
}

function ResearchClaims({ claims, symbol }) {
  return claims.map((claim) => (
    <div key={claim.id} className="text-sm">
      <p>{claimText(claim.statement)}</p>
      <ul className="mt-1 space-y-1 text-xs text-muted">
        {claim.sources.map((source) => (
          <li key={`${claim.id}-${source.extraction_id}`}>
            {source.period_end_jalali} · {source.scope
              ? source.statement_kind === "balance_sheet"
                ? tr("{scope} balance sheet cells: assets {a}, liabilities {b}, equity {c}", { scope: tr(source.scope), a: source.source_coordinates.total_assets.address, b: source.source_coordinates.total_liabilities.address, c: source.source_coordinates.total_equity.address })
                : tr("{scope} income statement cells: {a}, {b}", { scope: tr(source.scope), a: source.source_coordinates.revenue.address, b: source.source_coordinates.net_profit.address })
              : tr("sales table row {r}, column {c}", { r: source.source_coordinates.row ?? "?", c: source.source_coordinates.column ?? "?" })} · {tr("file {id}", { id: source.artifact_id })}
            {source.source_url && <> · <a href={source.source_url} target="_blank" rel="noopener noreferrer" className="text-accent underline">{tr("Codal filing")}</a></>}
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
        <p className="text-sm font-medium">{tr("Saved answer")} · {date(run.created_at)}</p>
        <p className="text-sm text-muted">{run.question}</p>
        {run.status === "answered" && (
          <p role="status" className={`text-sm ${run.evidence_state === "current" ? "text-muted" : "text-[var(--c-critical-text)]"}`}>
            {run.evidence_state === "current"
              ? tr("The sources and calculation still match our current data. The archived file is checked when you download it.")
              : tr("This is an older answer: its sources have changed or cannot be rechecked. Do not use it as a current figure.")}
          </p>
        )}
        {run.claims?.length > 0 && <ResearchClaims claims={run.claims} symbol={symbol} />}
        {run.status !== "answered" && <p className="text-sm">{tr("No answer was given:")} {tr(RESEARCH_GAPS[run.failure_code] || "We could not get an answer this time. Please try again later.")}</p>}
      </div>
    ) : <p role="alert" className="text-sm">{tr("This saved answer belongs to {a}, not {b}.", { a: run.symbol, b: symbol })}</p>}
  </Async>;
}

function ResearchPanel({ symbol }) {
  const [params, setParams] = useSearchParams();
  const runId = params.get("run");
  const settings = useApi(() => researchSettings(), []);
  const [question, setQuestion] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

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
      setResult(await runResearch(symbol, question.trim(), settings.data.default_run_usd));
    } catch (caught) {
      setError(caught.message || tr("Research could not complete."));
    } finally {
      setWorking(false);
    }
  }

  return (
    <Card
      title="Ask about verified sales and statements"
      subtitle="An AI model answers from figures checked against the filings. It declines questions about dollars, other companies or valuation."
      testId="explore-research"
    >
      <Async {...settings} testId="explore-research-settings">
        {(config) => (
          <div className="space-y-3">
            <p className="text-xs text-muted">
              {config.provider_status === "ready"
                ? tr("Your question goes to an AI service; your holdings do not. There is a daily limit on questions.")
                : tr("Research questions are not available yet. The verified charts above still work.")}
            </p>
            <form className="space-y-3" onSubmit={submit}>
              <Field label="Your question" hint="For example: Which verified month had the highest sales?">
                <Textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={3} minLength={3} maxLength={600} required className="w-full" label="Sales research question" data-testid="explore-research-question" />
              </Field>
              <div>
                <Button type="submit" variant="primary" disabled={working || config.provider_status !== "ready"} data-testid="explore-research-submit">
                  {working ? "Checking evidence…" : "Run research"}
                </Button>
              </div>
            </form>
            {error && <p role="alert" className="text-sm text-[var(--c-critical-text)]">{error}</p>}
            {runId && (/^[1-9]\d*$/.test(runId)
              ? <SavedResearch symbol={symbol} runId={runId} />
              : <p role="alert" className="text-sm">{tr("This saved answer link is not valid.")}</p>)}
            {result && (
              <div className="space-y-3 border-t border-border pt-3" data-testid="explore-research-result">
                {result.claims?.length ? <ResearchClaims claims={result.claims} symbol={symbol} /> : <p className="text-sm">{tr(RESEARCH_GAPS[result.reason] || "The verified evidence cannot answer this question.")}</p>}
                {result.run_id && <Link to={`/research?${new URLSearchParams({ view: "companies", symbol, run: String(result.run_id) })}`} onClick={() => setResult(null)} className="text-sm text-accent underline">{tr("Open saved answer with a fresh source check")}</Link>}
                {result.coverage && <p className="text-xs text-muted">{tr("Latest filings checked / withheld: sales {a} / {b}, income {c} / {d}, balance sheet {e} / {f}.", { a: result.coverage.verified_periods, b: result.coverage.withheld_periods, c: result.coverage.income_verified_periods, d: result.coverage.income_withheld_periods, e: result.coverage.balance_verified_periods, f: result.coverage.balance_withheld_periods })}</p>}
                <p className="text-xs text-muted">{tr("The numbers come from stored calculations; the model only chose which ones answer your question.")}</p>
              </div>
            )}
          </div>
        )}
      </Async>
    </Card>
  );
}

/** Follow or unfollow this company; the list lives under Research → Watchlist. */
function WatchButton({ symbol }) {
  const list = useApi(() => watchlist(), []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  // What the user just did beats the fetched list until the list is refetched.
  const [chosen, setChosen] = useState(null);
  const watched = chosen ?? (list.data?.results || []).some((r) => r.symbol === symbol);
  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError(false);
    try {
      if (watched) await unwatch(symbol); else await watch(symbol);
      setChosen(!watched);
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button variant="ghost" onClick={toggle} disabled={busy || list.loading} data-testid={watched ? "explore-watched" : "explore-watch"}>
      {error ? translate("Could not save — retry") : watched ? translate("On your watchlist · Remove") : translate("Watch")}
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
                {data.company.sector
                  ? translate("Industry is a current provider-reported {kind} label, observed {date}; it does not establish past membership.", lang, {
                    kind: translate(data.company.sector_source === "symbol_metadata" ? "symbol metadata" : "instrument catalog", lang),
                    date: date(data.company.sector_observed_at),
                  })
                  : translate("Industry is unavailable for this stock.", lang)}{" "}
                {translate("We only show sales, income and balance figures we could check against the company's Codal filing.", lang)}
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
                  series={[{ key: "close", name: `${symbol} (${tr("Rial")})` }]}
                  data={rows}
                  longTicks={rows.length > 180}
                  formatValue={rial}
                  formatAxis={rial}
                  label={tr("{symbol} daily closes in Rial", { symbol })}
                />
              ) : <Empty testId="explore-no-prices">No daily close in this period.</Empty>}
              <div className="mt-3 space-y-1 text-xs text-muted" data-testid="explore-price-evidence">
                <p>{translate("Source: {source}. {n} dated closes; {first} → {last} (Jalali).", lang, { source: price.source, n: price.points.length, first: price.first_date || "—", last: price.last_date || "—" })}</p>
                {price.halted_days > 0 && (
                  <p data-testid="explore-price-halted">
                    {translate("{n} days without a trade (a halt or market closure) are left out, not drawn as a flat price.", lang, { n: price.halted_days })}
                  </p>
                )}
                <p>
                  {translate("{n} days checked against a second price source; {m} differ by more than 1%.", lang, { n: price.paired_candle_days, m: price.candle_disagreements_over_1pct })}
                  {price.candle_disagreements_over_1pct > 0 && ` ${translate("Treat this chart as disputed until those days are reconciled.", lang)}`}
                </p>
                <p>{translate("Adjusted prices, USD conversion, and return comparisons are withheld pending source checks.", lang)}</p>
              </div>
            </Card>

            <Card
              title="Monthly sales"
              subtitle="Current-month reported sales in million Rial. A later unverified correction withholds the older value."
              testId="explore-monthly-sales"
              actions={<Badge variant={sales.withheld_periods ? "warn" : sales.points.length ? "good" : "warn"}>{tr("{a} checked · {b} withheld", { a: sales.verified_periods, b: sales.withheld_periods })}</Badge>}
            >
              {salesRows.length ? (
                <MultiLineTrend
                  series={[{ key: "sales", name: `${symbol} (${tr("million Rial")})` }]}
                  data={salesRows}
                  formatValue={(value) => `${num(value, 0)} ${tr("million Rial")}`}
                  formatAxis={(value) => num(value, 0)}
                  label={tr("{symbol} monthly sales in million Rial", { symbol })}
                />
              ) : <Empty testId="explore-no-monthly-sales">No monthly sales could be checked against the filings in this period.</Empty>}
              <p className="mt-3 text-xs text-muted">{tr("Checked means the arithmetic, period, unit and archived file were verified against the filing. It is not an audit of the company’s accounts.")}</p>
              {sales.points.length > 0 && (
                <details className="mt-3 text-sm" data-testid="explore-sales-evidence">
                  <summary className="cursor-pointer">{tr("Inspect source cells")}</summary>
                  <ul className="mt-2 max-h-56 space-y-2 overflow-y-auto">
                    {sales.points.slice().reverse().map((point) => (
                      <li key={`${point.period_end_jalali}-${point.extraction_id}`} className="border-b border-border pb-2">
                        <span>{point.period_end_jalali}: {num(Number(point.value), 0)} {tr("million Rial")}</span>
                        <span className="block text-xs text-muted">{tr("Filed {date}", { date: point.published_jalali || tr("date unavailable") })}{point.is_correction ? ` · ${tr("correction")}` : ""}</span>
                        {point.source_url && <a className="text-accent underline underline-offset-2" href={point.source_url} target="_blank" rel="noopener noreferrer">{tr("Original Codal filing")}</a>}
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
              actions={<Badge variant={income?.withheld_periods ? "warn" : income?.points?.length ? "good" : "warn"}>{tr("{a} checked · {b} withheld", { a: income?.verified_periods || 0, b: income?.withheld_periods || 0 })}</Badge>}
            >
              {income?.points?.length ? (
                <div className="max-h-96 space-y-3 overflow-y-auto" data-testid="explore-income-evidence">
                  {income.points.slice().reverse().map((point) => (
                    <div key={`${point.period_end_jalali}-${point.scope}`} className="border-b border-border pb-3 text-sm">
                      <p className="font-medium">{point.period_start_jalali} → {point.period_end_jalali} · {tr(point.scope)} · {tr(point.audited ? "audited" : "unaudited")}</p>
                      <p>{tr("Revenue {a} · net profit {b} million Rial · net margin {c}%", { a: reportedAmount(point.revenue), b: reportedAmount(point.net_profit), c: point.net_margin_pct })}</p>
                      <p className="text-xs text-muted">{tr("Filed {date}", { date: point.published_jalali || tr("date unavailable") })}{point.is_correction ? ` · ${tr("correction")}` : ""}</p>
                      <details className="text-xs text-muted"><summary className="cursor-pointer">{tr("Where this comes from")}</summary>{tr("file {id}", { id: point.artifact_id })} · {tr("revenue cell {a}, profit cell {b}", { a: point.source_coordinates.revenue.address, b: point.source_coordinates.net_profit.address })}</details>
                      {point.source_url && <a className="text-xs text-accent underline underline-offset-2" href={point.source_url} target="_blank" rel="noopener noreferrer">{tr("Original Codal filing")}</a>}
                      {" "}<ArchivedFiling symbol={symbol} point={point} days={Number(days)} />
                    </div>
                  ))}
                </div>
              ) : <Empty testId="explore-no-income">No income statement could be checked against the filings in this period.</Empty>}
              <p className="mt-3 text-xs text-muted">{tr("The source cells and arithmetic were checked against the archived filing. This does not audit the company’s accounts or make interim and annual figures comparable.")}</p>
            </Card>
            <Card
              title="Assets, liabilities, and equity"
              subtitle="Point-in-time balance sheet amounts in million Rial. Standalone and consolidated filings stay separate; a newer unverified filing withholds an older balance."
              testId="explore-balance"
              actions={<Badge variant={balance?.withheld_periods ? "warn" : balance?.points?.length ? "good" : "warn"}>{tr("{a} checked · {b} withheld", { a: balance?.verified_periods || 0, b: balance?.withheld_periods || 0 })}</Badge>}
            >
              {balance?.points?.length ? (
                <div className="max-h-96 space-y-3 overflow-y-auto" data-testid="explore-balance-evidence">
                  {balance.points.slice().reverse().map((point) => (
                    <div key={`${point.period_end_jalali}-${point.scope}`} className="border-b border-border pb-3 text-sm">
                      <p className="font-medium">{point.period_end_jalali} · {tr(point.scope)} · {tr(point.audited ? "audited" : "unaudited")}</p>
                      <p>{tr("Assets {a} · liabilities {b} · equity {c} million Rial", { a: reportedAmount(point.values.total_assets), b: reportedAmount(point.values.total_liabilities), c: reportedAmount(point.values.total_equity) })}</p>
                      <p>{tr("Cash {a} · short-term borrowings {b} · long-term borrowings {c} million Rial", { a: reportedAmount(point.values.cash), b: reportedAmount(point.values.short_term_borrowings), c: reportedAmount(point.values.long_term_borrowings) })}</p>
                      <p className="text-xs text-muted">{tr("Filed {date}", { date: point.published_jalali || tr("date unavailable") })}{point.is_correction ? ` · ${tr("correction")}` : ""}</p>
                      <details className="text-xs text-muted"><summary className="cursor-pointer">{tr("Where this comes from")}</summary>{tr("file {id}", { id: point.artifact_id })} · {tr("assets cell {a}, liabilities cell {b}, equity cell {c}", { a: point.source_coordinates.total_assets.address, b: point.source_coordinates.total_liabilities.address, c: point.source_coordinates.total_equity.address })}</details>
                      {point.source_url && <a className="text-xs text-accent underline underline-offset-2" href={point.source_url} target="_blank" rel="noopener noreferrer">{tr("Original Codal balance sheet")}</a>}
                      {" "}<ArchivedFiling symbol={symbol} point={point} days={Number(days)} />
                    </div>
                  ))}
                </div>
              ) : <Empty testId="explore-no-balance">No balance sheet could be checked against the filings in this period.</Empty>}
              <p className="mt-3 text-xs text-muted">{tr("The archived figures add up: assets = liabilities + equity. This does not audit the company’s accounts or imply a valuation.")}</p>
            </Card>
            <ResearchPanel symbol={symbol} />

            <div className="grid gap-5 lg:grid-cols-2">
              <Card title="Data we hold" subtitle="How much we have stored so far, not a promise that every value is right." testId="explore-coverage">
                <ul className="space-y-3">
                  {data.coverage.map((row) => (
                    <li key={row.endpoint} className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2 text-sm">
                      <span>{tr(COVERAGE_LABELS[row.endpoint] || row.endpoint)}</span>
                      <span className="flex items-center gap-2">
                        <span className="text-muted">{tr("{n} rows", { n: num(row.stored_rows, 0) })}</span>
                        <Badge variant={row.verified_complete ? "good" : "warn"}>
                          {row.verified_complete ? "Coverage checked" : "Coverage incomplete"}
                        </Badge>
                      </span>
                      {row.last_success_at && <span className="w-full text-xs text-muted">{tr("Last updated {date}", { date: date(row.last_success_at) })}</span>}
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
                        <span className="mt-1 block text-xs text-muted">{item.published_jalali || tr("Date unavailable")} · {item.category || tr("Unclassified")}</span>
                      </li>
                    ))}
                  </ul>
                ) : <Empty testId="explore-no-disclosures">No stored disclosure for this symbol.</Empty>}
              </Card>
            </div>
            {data.company.metadata_updated_at && (
              <p className="text-xs text-muted">{tr("Company list last updated {date}.", { date: date(data.company.metadata_updated_at) })}</p>
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

  // Search as you type. The box used to do nothing until Search was pressed,
  // which on a phone reads as a dead field.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(draft.trim()), 300);
    return () => clearTimeout(timer);
  }, [draft]);
  // With a company already open and nothing typed, the 30 alphabetical tickers
  // only push its page below the fold.
  const showList = Boolean(query) || !symbol;

  return (
    <div className="space-y-5">
      <PageHeader title="Explore companies" subtitle="Look up any TSE company: its prices, trading days and filings." />
      <Card title="Find a company" testId="explore-search">
        <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); setQuery(draft.trim()); }}>
          <Input label="Search by TSE symbol or company name" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Symbol or company name" maxLength={100} className="min-w-52 flex-1" data-testid="explore-query" />
          <Button type="submit" variant="ghost" data-testid="explore-submit">Search</Button>
        </form>
        {showList && <Async {...results} testId="explore-results" empty="No stocks match this search.">
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
          ) : (
            // Before anything is typed there was no search to "match", so the
            // empty default list says what is actually the case.
            <Empty testId="explore-no-results">
              {query ? "No company matches this search. Try its ticker, like فولاد." : "No companies are listed yet."}
            </Empty>
          )}
        </Async>}
      </Card>
      {symbol ? <Company key={symbol} symbol={symbol} /> : (
        <Empty testId="explore-prompt">Pick a company above to see its prices and filings.</Empty>
      )}
    </div>
  );
}

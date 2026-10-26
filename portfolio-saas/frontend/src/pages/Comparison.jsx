import { useEffect, useMemo, useState } from "react";
import { translate, useT, useLang } from "../i18n.js";
import { comparison, ensureAsset, searchAssetCatalog } from "../api.js";
import { MultiLineTrend } from "../components/charts.jsx";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import {
  Async,
  Card,
  Button,
  ErrorState,
  Input,
  PageHeader,
  Select,
  StatTile,
  Table,
  Tabs,
} from "../components/ui.jsx";
import { humanize, num, rial, signedPct, toman } from "../format.js";
import { useApi } from "../useApi.js";

// 0 means "as far back as my own purchases go", which is the answer this page is
// usually asked for. The others are the windows the rest of the app already
// offers, so a user does not learn a second vocabulary of ranges here.
const RANGES = [
  { value: "0", label: "All" },
  { value: "365", label: "1Y" },
  { value: "180", label: "6M" },
  { value: "90", label: "90D" },
];

// Whole units are noise on 27M shares, but 0.25 BTC is not "0".
const units = (n) => num(n, n >= 100 ? 0 : 2);

// The unit travels with every per-unit price the API sends: TSE prices are
// Rial on every other screen, so a break-even shown in Toman would read as a
// tenth of the quote.
const price = (p) => (!p ? "—" : p.unit === "IRR" ? rial(p.value) : toman(p.value));

function Verdict({ subject }) {
  const lang = useLang();
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <StatTile label="Put in" value={toman(subject.invested_tomans)} testId="comparison-invested" />
      <StatTile
        label="Worth today"
        value={toman(subject.value_tomans)}
        sub={subject.end_date ? `${translate(subject.priced_live ? "Live price" : "Closing price", lang)} · ${subject.end_date}` : undefined}
        testId="comparison-actual"
      />
      <StatTile
        label="Price now"
        value={price(subject.price)}
        sub={`${units(subject.units)} × ${subject.label}`}
        testId="comparison-price"
      />
    </div>
  );
}

// The catalog classes a target can come from. Same search the add-holding
// wizard uses, so anything that can be held can be compared against.
const CLASSES = [
  { value: "Gold", label: "Gold & coins" },
  { value: "Cash", label: "Currency" },
  { value: "Stock", label: "Stocks" },
  { value: "Crypto", label: "Crypto" },
];

function HoldingPicker({ value, onChange, holdings }) {
  const t = useT();
  return (
    <label className="flex flex-col gap-1 text-xs font-medium tracking-wide text-muted uppercase">
      {t("What you did")}
      <Select
        label="What you did"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid="comparison-subject"
        className="min-w-44"
      >
        {holdings.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </Select>
    </label>
  );
}

function TargetSearch({ onAdd, exclude }) {
  const lang = useLang();
  const [cls, setCls] = useState("Gold");
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 200);
    return () => clearTimeout(timer);
  }, [query]);
  const hits = useApi(() => searchAssetCatalog(cls, debounced), [cls, debounced]);

  // A warehouse instrument nobody holds yet has no catalog row; the wizard's
  // own endpoint makes the shared one, so the comparison only ever sees keys.
  const pick = async (row) => {
    if (row.key) return onAdd(row.key, row.name_fa || row.name);
    setBusy(true);
    setError(null);
    try {
      const asset = await ensureAsset(row.source, row.symbol);
      onAdd(asset.key, asset.name_fa || asset.name);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const rows = (hits.data || []).filter((row) => !row.key || !exclude.includes(row.key));
  return (
    <div className="rounded-lg border border-border p-3" data-testid="comparison-search">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Tabs options={CLASSES} value={cls} onChange={setCls} label="Asset class" testId="comparison-class" />
        <Input
          label="Search"
          placeholder="Search by name or symbol…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="min-w-48 flex-1"
          data-testid="comparison-search-input"
        />
      </div>
      {error && <ErrorState error={error} testId="comparison-search-error" />}
      <div className="flex flex-wrap gap-2">
        {rows.slice(0, 12).map((row) => (
          <Button
            key={`${row.source}:${row.symbol}:${row.key}`}
            onClick={() => pick(row)}
            disabled={busy}
            data-testid="comparison-search-hit"
          >
            <bdi>{row.name_fa || row.name}</bdi>
          </Button>
        ))}
        {!hits.loading && !rows.length && (
          <span className="text-xs text-muted">{translate("Nothing matches.", lang)}</span>
        )}
      </div>
    </div>
  );
}

// The server's own cap (MAX_TARGETS in services/comparison.py). The page asks one
// target per request, so the server never saw the count and 7+ chips piled up.
const MAX_TARGETS = 6;

function Chips({ chosen, labels, onRemove }) {
  const t = useT();
  return (
    <div className="flex flex-wrap gap-2" data-testid="comparison-targets">
      {chosen.map((key) => (
        <button
          key={key}
          type="button"
          onClick={() => onRemove(key)}
          aria-label={`${t("Remove")} ${labels[key] || key}`}
          className="rounded-full border border-border bg-panel-2 px-3 py-1 text-sm"
        >
          <bdi>{labels[key] || key}</bdi>
          <span aria-hidden="true"> ×</span>
        </button>
      ))}
    </div>
  );
}

// One target at a time. The server caps a user at two concurrent analytics
// requests, and asking for every target in one call recomputed all of them on
// each add or remove -- the table went blank, and a removal left the old rows
// up until the whole recomputation came back.
function RowLoader({ activeId, subject, target, days, onSettled }) {
  const result = useApi(
    () => comparison(activeId, { subject, targets: [target], days }),
    [activeId, subject, target, days],
    { timeoutMs: 60000 }
  );
  useEffect(() => {
    if (result.loading) return;
    onSettled(result.error ? { error: result.error } : { data: result.data });
  }, [result.loading, result.data, result.error]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

function Rows({ subject, rows }) {
  const lang = useLang();
  const pending = (r) => r.loading;
  const dash = (r) => (pending(r) ? "…" : "—");
  const columns = [
    {
      key: "label",
      header: "Instead",
      mobile: "title",
      render: (r) => (
        <div>
          <bdi>{r.label}</bdi>
          {r.reason && (
            <div className="text-xs text-muted">
              {r.detail || humanize(r.reason)}
              {r.retry && (
                <Button variant="link" onClick={r.retry} data-testid="comparison-row-retry">
                  {translate("Retry", lang)}
                </Button>
              )}
            </div>
          )}
        </div>
      ),
    },
    {
      key: "value",
      header: "Worth today",
      align: "right",
      mobile: "value",
      // A target whose series stopped ends its row on its last real price;
      // that value is not today's, so the row says which day it is.
      render: (r) =>
        r.value_tomans == null ? dash(r) : (
          <div>
            {toman(r.value_tomans)}
            {subject?.end_date && r.end_date < subject.end_date && (
              <div className="text-xs text-muted" data-testid="comparison-row-as-of">
                {translate("as of {date}", lang, { date: r.end_date })}
              </div>
            )}
          </div>
        ),
    },
    {
      key: "lead",
      // Two "Difference" columns in English; Persian names which is which.
      header: subject ? translate("{name} vs it", lang, { name: subject.label }) : "Difference|percent",
      align: "right",
      mobile: "meta",
      render: (r) =>
        r.difference_pct == null ? dash(r) : (
          <span className={r.difference_tomans >= 0 ? "text-[var(--c-good-text)]" : "text-[var(--c-critical-text)]"}>
            {signedPct(r.difference_pct / 100)}
          </span>
        ),
    },
    {
      key: "difference",
      header: "Difference|Toman",
      align: "right",
      render: (r) => (r.difference_tomans == null ? dash(r) : toman(Math.abs(r.difference_tomans))),
    },
    {
      key: "breakeven",
      header: "Break-even price",
      align: "right",
      mobile: "meta",
      render: (r) => (r.breakeven_price ? price(r.breakeven_price) : dash(r)),
    },
    {
      key: "units",
      header: "Would hold",
      align: "right",
      render: (r) => (r.units == null ? dash(r) : units(r.units)),
    },
  ];
  return (
    <>
      <Table
        columns={columns}
        rows={rows}
        rowKey={(r) => r.key}
        mobileCards
        caption={translate("What the same money would be worth in each alternative", lang)}
        testId="comparison-table"
      />
      {subject && (
        <p className="mt-2 text-xs text-muted" data-testid="comparison-breakeven-note">
          {translate("Break-even price: what {name} would have to trade at for your position to be worth exactly the same as the alternative.", lang, { name: subject.label })}
        </p>
      )}
    </>
  );
}

function Chart({ row }) {
  // The API returns one point list per curve; the chart wants one row per date
  // with a column per curve, so they are zipped on the date they share.
  const rows = useMemo(() => {
    const byDate = new Map();
    row.series.forEach((curve, index) => {
      for (const point of curve.points) {
        const entry = byDate.get(point.date) || { x: point.date };
        entry[`s${index}`] = point.value;
        byDate.set(point.date, entry);
      }
    });
    return [...byDate.values()].sort((a, b) => a.x.localeCompare(b.x));
  }, [row]);

  return (
    <>
      <MultiLineTrend
        series={row.series.map((curve, index) => ({ key: `s${index}`, name: curve.label }))}
        data={rows}
        longTicks={rows.length > 400}
        label={row.series.map((c) => c.label).join(" versus ")}
      />
      {/* The sentence, when the server sent one: which day the data stops on
          and what the chart did about it. */}
      {(row.warnings || []).map((w) => (
        <p key={`${w.key}-${w.reason}`} className="mt-1 text-xs text-muted" data-testid="comparison-warning">
          {w.detail || humanize(w.reason)}.
        </p>
      ))}
    </>
  );
}

export default function Comparison() {
  const lang = useLang();
  const { activeId } = usePortfolio();
  const [range, setRange] = useState("0");
  const [subject, setSubject] = useState("");
  const [chosen, setChosen] = useState([]);
  const [labels, setLabels] = useState({});
  const [adding, setAdding] = useState(false);
  const [shown, setShown] = useState("");
  // Settled answers by portfolio|holding|window|target. Kept across adds and
  // removals, so taking a target out and putting it back costs nothing.
  const [cache, setCache] = useState({});

  const choices = useApi(() => comparison(activeId), [activeId]);
  const holdings = useMemo(() => choices.data?.holdings || [], [choices.data]);
  const omitted = choices.data?.omitted_holdings || [];

  // Open on a holding that has money to replay: one recorded without a price
  // can only answer "no recorded cost".
  useEffect(() => {
    if (holdings.some((h) => h.key === subject)) return;
    setSubject((holdings.find((h) => h.priced) || holdings[0])?.key || "");
  }, [holdings, subject]);

  useEffect(() => {
    if (!choices.data) return;
    setLabels((cur) => ({
      ...Object.fromEntries((choices.data.targets || []).map((o) => [o.key, o.label])),
      ...cur,
    }));
    setChosen((cur) => (cur.length ? cur : choices.data.default_targets || []));
  }, [choices.data]);

  const days = Number(range) || undefined;
  const slot = (target) => `${activeId}|${subject}|${range}|${target}`;
  const asked = chosen.filter((key) => key !== subject);
  const next = subject ? asked.find((key) => !cache[slot(key)]) : undefined;

  const settled = asked.map((key) => cache[slot(key)]).filter(Boolean);
  // A 400 is the holding itself (no recorded cost, nothing to replay): the same
  // for every target, so it is the page's answer. Target refusals come back as
  // rows with a 200. Anything else -- the per-user concurrency cap, a timeout,
  // a 5xx -- belongs to the one row it hit and can be retried there.
  const failure = settled.find((entry) => entry.error?.status === 400)?.error;
  const retry = (key) =>
    setCache((cur) => {
      const nextCache = { ...cur };
      delete nextCache[slot(key)];
      return nextCache;
    });
  const subjectData = settled.find((entry) => entry.data)?.data.subject;
  const rows = asked.map((key) => {
    const entry = cache[slot(key)];
    const label = labels[key] || key;
    if (entry?.error) {
      return { key, label, reason: "failed", detail: entry.error.message, retry: () => retry(key) };
    }
    const row = entry?.data?.rows?.[0];
    return row ? { ...row, label: row.label || label } : { key, label, loading: true };
  });
  const answered = rows.filter((r) => r.series);
  const chartRow = answered.find((r) => r.key === shown) || answered[0];

  const add = (key, label) => {
    if (!chosen.includes(key) && chosen.length >= MAX_TARGETS) return;
    setLabels((cur) => ({ ...cur, [key]: label || cur[key] || key }));
    setChosen((cur) => (cur.includes(key) ? cur : [...cur, key]));
    setShown(key);
  };

  return (
    <div>
      <PageHeader title="Compare" />
      <Card
        title="Same money elsewhere"
        subtitle="Every purchase you actually made — same dates, same amounts — replayed into something else."
        testId="comparison-panel"
        actions={
          <Tabs options={RANGES} value={range} onChange={setRange} label="Window" testId="comparison-range" />
        }
      >
        <Async {...choices} testId="comparison-choices">
          {() => (
            <>
              {next && (
                <RowLoader
                  key={slot(next)}
                  activeId={activeId}
                  subject={subject}
                  target={next}
                  days={days}
                  onSettled={(entry) => setCache((cur) => ({ ...cur, [slot(next)]: entry }))}
                />
              )}
              {!holdings.length ? (
                <p className="text-sm text-muted" data-testid="comparison-no-holdings">
                  {translate("This portfolio has no priced positions to compare yet. Record a purchase in the Ledger first.", lang)}
                </p>
              ) : (
                <>
                  <div className="mb-4 flex flex-wrap items-end gap-4">
                    <HoldingPicker value={subject} onChange={setSubject} holdings={holdings} />
                  </div>
                  <div className="mb-4 flex flex-wrap items-center gap-2">
                    <Chips
                      chosen={chosen}
                      labels={labels}
                      onRemove={(key) => setChosen((cur) => cur.filter((k) => k !== key))}
                    />
                    <Button onClick={() => setAdding((v) => !v)} data-testid="comparison-add">
                      {adding ? translate("Done", lang) : translate("+ Compare with…", lang)}
                    </Button>
                    {chosen.length >= MAX_TARGETS && (
                      <span className="text-xs text-muted" data-testid="comparison-max">
                        {translate("Up to {n} at a time — remove one to add another.", lang, { n: MAX_TARGETS })}
                      </span>
                    )}
                  </div>
                  {adding && (
                    <div className="mb-4">
                      <TargetSearch onAdd={add} exclude={[subject, ...chosen]} />
                    </div>
                  )}
                </>
              )}
              {/* Named rather than simply absent: a reader who owns a house and
                  three gold bars was scanning a list that never mentioned them
                  and had no way to tell whether that was a bug. */}
              {!!omitted.length && (
                <p className="mb-4 text-xs text-muted" data-testid="comparison-omitted">
                  {translate("Not available to compare:", lang)}{" "}
                  {omitted.map((o, i) => (
                    <span key={o.key}>
                      {i > 0 && "; "}
                      <bdi className="text-text">{o.label}</bdi> — {translate(o.reason, lang)}
                    </span>
                  ))}
                  .
                </p>
              )}
              {failure ? (
                // Most refusals here are data limits with a sentence attached
                // — a position with no recorded cost has nothing to replay.
                <ErrorState error={failure} testId="comparison-result" />
              ) : asked.length ? (
                <div data-testid="comparison-result">
                  {subjectData && <Verdict subject={subjectData} />}
                  <div className="mt-5">
                    <Rows subject={subjectData} rows={rows} />
                  </div>
                  {chartRow && (
                    <div className="mt-6">
                      {answered.length > 1 && (
                        <div className="mb-3">
                          <Tabs
                            options={answered.map((r) => ({ value: r.key, label: r.label }))}
                            value={chartRow.key}
                            onChange={setShown}
                            label="Chart"
                            testId="comparison-chart-pick"
                          />
                        </div>
                      )}
                      <Chart row={chartRow} />
                    </div>
                  )}
                </div>
              ) : (
                holdings.length > 0 && (
                  <p className="text-sm text-muted" data-testid="comparison-prompt">
                    {translate("Pick something to compare it against.", lang)}
                  </p>
                )
              )}
            </>
          )}
        </Async>
      </Card>
    </div>
  );
}

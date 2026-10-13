import { useEffect, useMemo, useState } from "react";
import { translate, useT, useLang } from "../i18n.js";
import { comparison } from "../api.js";
import { MultiLineTrend } from "../components/charts.jsx";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import {
  Async,
  Card,
  ErrorState,
  PageHeader,
  Select,
  StatTile,
  Table,
  Tabs,
} from "../components/ui.jsx";
import { dateTime, humanize, num, rial, signedPct, toman } from "../format.js";
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

// The unit travels with every per-unit price the API sends: TSE prices are
// Rial on every other screen, so a break-even shown in Toman would read as a
// tenth of the quote.
const price = (p) => (!p ? "—" : p.unit === "IRR" ? rial(p.value) : toman(p.value));

function Picker({ label, value, onChange, options, testId, placeholder = "Choose…" }) {
  const t = useT();
  return (
    <label className="flex flex-col gap-1 text-xs font-medium tracking-wide text-muted uppercase">
      {t(label)}
      <Select
        label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid={testId}
        className="min-w-44"
      >
        <option value="">{t(placeholder)}</option>
        {options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </Select>
    </label>
  );
}

function Chips({ chosen, targets, onRemove }) {
  const t = useT();
  const label = (key) => targets.find((o) => o.key === key)?.label || key;
  return (
    <div className="flex flex-wrap gap-2" data-testid="comparison-targets">
      {chosen.map((key) => (
        <button
          key={key}
          type="button"
          onClick={() => onRemove(key)}
          // The last chip stays: a table with no rows asks nothing.
          disabled={chosen.length === 1}
          aria-label={`${t("Remove")} ${label(key)}`}
          className="rounded-full border border-border bg-panel-2 px-3 py-1 text-sm disabled:opacity-60"
        >
          <bdi>{label(key)}</bdi>
          {chosen.length > 1 && <span aria-hidden="true"> ×</span>}
        </button>
      ))}
    </div>
  );
}

function Verdict({ subject }) {
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <StatTile label="Put in" value={toman(subject.invested_tomans)} testId="comparison-invested" />
      <StatTile label="Worth today" value={toman(subject.value_tomans)} testId="comparison-actual" />
      <StatTile
        label="Price now"
        value={price(subject.price)}
        sub={`${num(subject.units, 0)} × ${subject.label}`}
        testId="comparison-price"
      />
    </div>
  );
}

function Rows({ data }) {
  const lang = useLang();
  const columns = [
    {
      key: "label",
      header: "Instead",
      mobile: "title",
      render: (r) => (
        <div>
          <bdi>{r.label}</bdi>
          {r.reason && (
            <div className="text-xs text-muted">{r.detail || humanize(r.reason)}</div>
          )}
        </div>
      ),
    },
    {
      key: "value",
      header: "Worth today",
      align: "right",
      mobile: "value",
      render: (r) => (r.reason ? "—" : toman(r.value_tomans)),
    },
    {
      key: "lead",
      header: `${data.subject.label} vs it`,
      align: "right",
      mobile: "meta",
      render: (r) =>
        r.reason ? "—" : (
          <span className={r.difference_tomans >= 0 ? "text-[var(--c-good-text)]" : "text-[var(--c-critical-text)]"}>
            {signedPct(r.difference_pct / 100)}
          </span>
        ),
    },
    {
      key: "difference",
      header: "Difference",
      align: "right",
      render: (r) => (r.reason ? "—" : toman(Math.abs(r.difference_tomans))),
    },
    {
      key: "breakeven",
      header: "Break-even price",
      align: "right",
      mobile: "meta",
      render: (r) => (r.reason ? "—" : price(r.breakeven_price)),
    },
    {
      key: "units",
      header: "Would hold",
      align: "right",
      render: (r) => (r.reason ? "—" : num(r.units, r.units >= 100 ? 0 : 2)),
    },
  ];
  return (
    <>
      <Table
        columns={columns}
        rows={data.rows}
        rowKey={(r) => r.key}
        mobileCards
        caption={translate("What the same money would be worth in each alternative", lang)}
        testId="comparison-table"
      />
      <p className="mt-2 text-xs text-muted" data-testid="comparison-breakeven-note">
        {translate("Break-even price: what {name} would have to trade at for your position to be worth exactly the same as the alternative.", lang, { name: data.subject.label })}
      </p>
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

function Result({ data }) {
  const answered = data.rows.filter((r) => !r.reason);
  const [shown, setShown] = useState(answered[0]?.key);
  const row = answered.find((r) => r.key === shown) || answered[0];
  return (
    <>
      <p className="mb-3 text-xs text-muted" data-testid="comparison-as-of">
        Prices through {data.subject.end_date}.
        {data.as_of ? ` Computed ${dateTime(data.as_of)}.` : ""}
      </p>
      <Verdict subject={data.subject} />
      <div className="mt-5">
        <Rows data={data} />
      </div>
      {row && (
        <div className="mt-6">
          {answered.length > 1 && (
            <div className="mb-3">
              <Tabs
                options={answered.map((r) => ({ value: r.key, label: r.label }))}
                value={row.key}
                onChange={setShown}
                label="Chart"
                testId="comparison-chart-pick"
              />
            </div>
          )}
          <Chart row={row} />
        </div>
      )}
    </>
  );
}

export default function Comparison() {
  const lang = useLang();
  const { activeId } = usePortfolio();
  const [range, setRange] = useState("0");
  const [subject, setSubject] = useState("");
  const [chosen, setChosen] = useState([]);

  const choices = useApi(() => comparison(activeId), [activeId]);
  // Memoized: the effects below depend on these, and `?.x || []` is a new
  // array every render.
  const holdings = useMemo(() => choices.data?.holdings || [], [choices.data]);
  const targets = useMemo(() => choices.data?.targets || [], [choices.data]);
  const omitted = choices.data?.omitted_holdings || [];

  // Switching portfolio can strip the holding that was selected; leaving a stale
  // key in place would ask the server about something this portfolio never held.
  useEffect(() => {
    if (subject && !holdings.some((h) => h.key === subject)) setSubject("");
    else if (!subject && holdings.length) setSubject(holdings[0].key);
  }, [holdings, subject]);

  useEffect(() => {
    const offered = new Set(targets.map((o) => o.key));
    setChosen((cur) => {
      const kept = cur.filter((key) => offered.has(key));
      return kept.length ? kept : choices.data?.default_targets || [];
    });
  }, [targets, choices.data]);

  // The holding itself is never its own alternative.
  const asked = chosen.filter((key) => key !== subject);
  const ready = !!subject && asked.length > 0;
  const result = useApi(
    () =>
      comparison(activeId, {
        subject,
        targets: asked,
        days: Number(range) || undefined,
      }),
    [activeId, subject, asked.join(","), range],
    { enabled: ready }
  );

  return (
    <div>
      <PageHeader title="Compare" />
      <Card
        title="Same money elsewhere"
        subtitle="Every purchase you actually made — same dates, same amounts — replayed into something else."
        testId="comparison-panel"
        actions={
          <Tabs
            options={RANGES}
            value={range}
            onChange={setRange}
            label="Window"
            testId="comparison-range"
          />
        }
      >
        <Async {...choices} testId="comparison-choices">
          {() => (
            <>
              <div className="mb-5 flex flex-wrap items-end gap-4">
                <Picker
                  label="What you did"
                  value={subject}
                  onChange={setSubject}
                  options={holdings}
                  testId="comparison-subject"
                />
                <Picker
                  label="Compare with"
                  value=""
                  placeholder="Add…"
                  onChange={(key) => key && setChosen((cur) => [...new Set([...cur, key])])}
                  options={targets.filter((o) => o.key !== subject && !chosen.includes(o.key))}
                  testId="comparison-target"
                />
              </div>
              {!!chosen.length && (
                <div className="mb-5">
                  <Chips
                    chosen={chosen}
                    targets={targets}
                    onRemove={(key) => setChosen((cur) => cur.filter((k) => k !== key))}
                  />
                </div>
              )}
              {!holdings.length && (
                <p className="text-sm text-muted" data-testid="comparison-no-holdings">
                  {translate("This portfolio has no priced positions to compare yet. Record a purchase in the Ledger first.", lang)}
                </p>
              )}
              {/* Named rather than simply absent: a reader who owns a house and
                  three gold bars was scanning a list that never mentioned them
                  and had no way to tell whether that was a bug. */}
              {!!omitted.length && (
                <p className="text-xs text-muted" data-testid="comparison-omitted">
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
              {ready ? (
                result.error ? (
                  // Most refusals here are data limits with a sentence attached
                  // — property has no market price, a position has no recorded
                  // cost — so the reason is the answer, not a failure.
                  <ErrorState error={result.error} testId="comparison-result" />
                ) : (
                  <Async {...result} testId="comparison-result">
                    {(data) => <Result key={`${subject}:${asked.join(",")}`} data={data} />}
                  </Async>
                )
              ) : (
                holdings.length > 0 && (
                  <p className="text-sm text-muted" data-testid="comparison-prompt">
                    {translate("Pick a holding and something to compare it against.", lang)}
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

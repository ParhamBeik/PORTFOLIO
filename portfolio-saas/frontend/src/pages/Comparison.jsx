import { useEffect, useMemo, useState } from "react";
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
  Tabs,
} from "../components/ui.jsx";
import { dateTime, humanize, indexPoint, num, toman } from "../format.js";
import { useApi } from "../useApi.js";

const MODES = [
  { value: "counterfactual", label: "Same money elsewhere" },
  { value: "holdings", label: "Two of mine" },
  { value: "benchmark", label: "Portfolio vs one asset" },
  { value: "lump_sum", label: "All at once" },
];

const BLURB = {
  counterfactual:
    "Every purchase you actually made — same dates, same amounts — replayed into something else.",
  holdings: "Two positions you really hold, side by side.",
  benchmark:
    "Your whole portfolio against one asset, both starting at 100 so the gap is relative growth.",
  lump_sum:
    "The same total, all of it on the first day, instead of spread over your real purchase dates.",
};

// 0 means "as far back as my own purchases go", which is the answer this page is
// usually asked for. The others are the windows the rest of the app already
// offers, so a user does not learn a second vocabulary of ranges here.
const RANGES = [
  { value: "0", label: "All" },
  { value: "365", label: "1Y" },
  { value: "180", label: "6M" },
  { value: "90", label: "90D" },
];

function Picker({ label, value, onChange, options, testId }) {
  return (
    <label className="flex flex-col gap-1 text-xs font-medium tracking-wide text-muted uppercase">
      {label}
      <Select
        label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid={testId}
        className="min-w-44"
      >
        <option value="">Choose…</option>
        {options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </Select>
    </label>
  );
}

function Verdict({ result }) {
  const s = result.summary;
  if (result.mode === "benchmark") {
    const lead = s.actual_change_pct - s.alternative_change_pct;
    return (
      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile
          label="Your portfolio"
          value={`${num(s.actual_change_pct, 1)}%`}
          valueTone={s.actual_change_pct >= 0 ? "good" : "critical"}
          testId="comparison-actual"
        />
        <StatTile
          label={result.series[1].label}
          value={`${num(s.alternative_change_pct, 1)}%`}
          testId="comparison-alternative"
        />
        <StatTile
          label={lead >= 0 ? "You are ahead by" : "You are behind by"}
          value={`${num(Math.abs(lead), 1)} pts`}
          valueTone={lead >= 0 ? "good" : "critical"}
          sub={`${s.start_date} → ${s.end_date}`}
          testId="comparison-difference"
        />
      </div>
    );
  }
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <StatTile
        label={result.series[0].label}
        value={toman(s.actual_end_tomans)}
        sub={
          s.invested_tomans != null ? `${toman(s.invested_tomans)} put in` : undefined
        }
        testId="comparison-actual"
      />
      <StatTile
        label={result.series[1].label}
        value={toman(s.alternative_end_tomans)}
        testId="comparison-alternative"
      />
      <StatTile
        label={s.difference_tomans >= 0 ? "You came out ahead" : "The road not taken wins"}
        value={toman(Math.abs(s.difference_tomans))}
        valueTone={s.difference_tomans >= 0 ? "good" : "critical"}
        sub={`${s.start_date} → ${s.end_date}`}
        testId="comparison-difference"
      />
    </div>
  );
}

function Chart({ result }) {
  // The API returns one point list per curve; the chart wants one row per date
  // with a column per curve, so they are zipped on the date they share.
  const rows = useMemo(() => {
    const byDate = new Map();
    result.series.forEach((curve, index) => {
      for (const point of curve.points) {
        const row = byDate.get(point.date) || { x: point.date };
        row[`s${index}`] = point.value;
        byDate.set(point.date, row);
      }
    });
    return [...byDate.values()].sort((a, b) => a.x.localeCompare(b.x));
  }, [result]);

  const isIndex = result.series[0].unit === "index";
  // A week either way is rounding — weekends, a market holiday, the day the
  // window opens on. Beyond that the range button asked for something the data
  // could not give, and saying nothing lets the axis imply it did.
  const { requested_days: asked, window_days: got } = result.summary;
  const shortfall = asked && got ? asked - got : 0;
  return (
    <>
      <MultiLineTrend
        series={result.series.map((curve, index) => ({
          key: `s${index}`,
          name: curve.label,
        }))}
        data={rows}
        longTicks={rows.length > 400}
        formatValue={isIndex ? indexPoint : undefined}
        formatAxis={isIndex ? indexPoint : undefined}
        label={result.series.map((c) => c.label).join(" versus ")}
      />
      {isIndex && (
        <p className="mt-2 text-xs text-muted" data-testid="comparison-index-note">
          Both lines start at 100, so the gap is relative growth over the window —
          not the amount of money in each.
        </p>
      )}
      {/* The sentence, when the server sent one. A warning that reads
          "euro cash: series ended." tells a reader something is wrong and
          nothing about what to do; the detail says which day the data stops on
          and what the chart did about it. */}
      {(result.warnings || []).map((w) => (
        <p
          key={`${w.key}-${w.reason}`}
          className="mt-1 text-xs text-muted"
          data-testid="comparison-warning"
        >
          {w.key}: {w.detail || humanize(w.reason)}.
        </p>
      ))}
      {shortfall > 7 && (
        <p className="mt-1 text-xs text-muted" data-testid="comparison-shortfall">
          You asked for {result.summary.requested_days} days and this covers{" "}
          {result.summary.window_days} — that is as much history as these two
          have in common.
        </p>
      )}
      {result.summary.truncated_to_days && (
        <p className="mt-1 text-xs text-muted" data-testid="comparison-truncated">
          Showing the last {result.summary.truncated_to_days} days — your
          portfolio's value is rebuilt day by day from the ledger, and that is as
          far back as it goes.
        </p>
      )}
    </>
  );
}

export default function Comparison() {
  const { activeId } = usePortfolio();
  const [mode, setMode] = useState("counterfactual");
  const [range, setRange] = useState("0");
  const [subject, setSubject] = useState("");
  const [target, setTarget] = useState("");

  const choices = useApi(() => comparison(activeId), [activeId]);
  // Memoized: both effects below depend on these, and `?.x || []` is a new
  // array every render, so each effect re-ran on every render instead of when
  // the portfolio actually changed.
  const holdings = useMemo(() => choices.data?.holdings || [], [choices.data]);
  const targets = useMemo(() => choices.data?.targets || [], [choices.data]);
  const omitted = choices.data?.omitted_holdings || [];
  const cryptoInvolved = [...holdings, ...targets].some(
    (row) => (row.key === subject || row.key === target) && row.asset_class === "Crypto"
  );

  // Switching portfolio can strip the holding that was selected; leaving a stale
  // key in place would ask the server about something this portfolio never held.
  useEffect(() => {
    if (subject && !holdings.some((h) => h.key === subject)) setSubject("");
    if (target && !targets.some((t) => t.key === target)) setTarget("");
  }, [holdings, targets]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!subject && holdings.length) setSubject(holdings[0].key);
  }, [holdings, subject]);

  const needsSubject = mode !== "benchmark";
  const ready = !!target && (!needsSubject || (!!subject && subject !== target));
  const result = useApi(
    () =>
      comparison(activeId, {
        mode,
        subject: needsSubject ? subject : undefined,
        target,
        days: Number(range) || undefined,
      }),
    [activeId, mode, subject, target, range, needsSubject],
    { enabled: ready }
  );

  return (
    <div>
      <PageHeader title="Compare" />
      {/* The page's one big choice sits above the card, as its own control.
          Inside the card's header it shared a line with the range and its
          own label repeated as the card title. */}
      <div className="mb-4">
        <Tabs
          options={MODES}
          value={mode}
          onChange={setMode}
          label="Comparison mode"
          testId="comparison-mode"
          grid
        />
      </div>
      <Card
        title="Pick what to compare"
        subtitle={BLURB[mode]}
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
                {needsSubject && (
                  <Picker
                    label={mode === "holdings" ? "Mine" : "What you did"}
                    value={subject}
                    onChange={(v) => {
                      setSubject(v);
                      if (v === target) setTarget("");
                    }}
                    options={holdings}
                    testId="comparison-subject"
                  />
                )}
                <Picker
                  label={mode === "holdings" ? "Against mine" : "Instead"}
                  value={target}
                  onChange={setTarget}
                  // Never the asset already picked on the left: comparing a
                  // holding with itself is an answer of "0" dressed up as one.
                  options={(mode === "holdings" ? holdings : targets).filter(
                    (o) => !needsSubject || o.key !== subject
                  )}
                  testId="comparison-target"
                />
              </div>
              {!holdings.length && (
                <p className="text-sm text-muted" data-testid="comparison-no-holdings">
                  This portfolio has no priced positions to compare yet. Record a
                  purchase in the Ledger first.
                </p>
              )}
              {/* "Two of mine" needs two. With one holding the picker offered
                  the same asset back and the page answered "cannot compare an
                  asset with itself", which reads as a bug rather than as the
                  shape of the portfolio. */}
              {mode === "holdings" && holdings.length === 1 && (
                <p className="text-sm text-muted" data-testid="comparison-one-holding">
                  This portfolio holds only {holdings[0].label}. Compare it
                  against something you don't own with the other modes above, or
                  record a second position first.
                </p>
              )}
              {/* Named rather than simply absent: a reader who owns a house and
                  three gold bars was scanning a list that never mentioned them
                  and had no way to tell whether that was a bug. */}
              {!!omitted.length && (
                <p className="text-xs text-muted" data-testid="comparison-omitted">
                  Not available to compare:{" "}
                  {omitted.map((o, i) => (
                    <span key={o.key}>
                      {i > 0 && "; "}
                      <span className="text-text">{o.label}</span> — {o.reason}
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
                    {(data) => (
                      <>
                        <p className="mb-3 text-xs text-muted" data-testid="comparison-as-of">
                          Prices through {data.summary?.end_date || "—"}.
                          {data.as_of ? ` Computed ${dateTime(data.as_of)}.` : ""}
                          {cryptoInvolved
                            ? " Crypto series start when this system began watching; there is no provider archive."
                            : ""}
                        </p>
                        <Verdict result={data} />
                        <div className="mt-5">
                          <Chart result={data} />
                        </div>
                      </>
                    )}
                  </Async>
                )
              ) : (
                // Silent when the message above already explained why there is
                // nothing to pick -- two prompts contradicting each other is
                // worse than one.
                holdings.length !== 1 || mode !== "holdings" ? (
                  <p className="text-sm text-muted" data-testid="comparison-prompt">
                    {needsSubject
                      ? "Pick a holding and something to compare it against."
                      : "Pick something to compare your portfolio against."}
                  </p>
                ) : null
              )}
            </>
          )}
        </Async>
      </Card>
    </div>
  );
}

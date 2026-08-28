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
import { indexPoint, num, signedToman, toman } from "../format.js";
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
        value={signedToman(s.difference_tomans)}
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
  const holdings = choices.data?.holdings || [];
  const targets = choices.data?.targets || [];

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
      <PageHeader
        title="Comparison"
        subtitle="What the same money would have done somewhere else."
      />
      <Card
        title={MODES.find((m) => m.value === mode).label}
        subtitle={BLURB[mode]}
        testId="comparison-panel"
        actions={
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <Tabs
              options={MODES}
              value={mode}
              onChange={setMode}
              label="Comparison mode"
              testId="comparison-mode"
            />
            <Tabs
              options={RANGES}
              value={range}
              onChange={setRange}
              label="Window"
              testId="comparison-range"
            />
          </div>
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
                    onChange={setSubject}
                    options={holdings}
                    testId="comparison-subject"
                  />
                )}
                <Picker
                  label={mode === "holdings" ? "Against mine" : "Instead"}
                  value={target}
                  onChange={setTarget}
                  options={mode === "holdings" ? holdings : targets}
                  testId="comparison-target"
                />
              </div>
              {!holdings.length && (
                <p className="text-sm text-muted" data-testid="comparison-no-holdings">
                  This portfolio has no priced positions to compare yet. Record a
                  purchase in the Ledger first.
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
                        <Verdict result={data} />
                        <div className="mt-5">
                          <Chart result={data} />
                        </div>
                      </>
                    )}
                  </Async>
                )
              ) : (
                <p className="text-sm text-muted" data-testid="comparison-prompt">
                  {needsSubject
                    ? "Pick a holding and something to compare it against."
                    : "Pick something to compare your portfolio against."}
                </p>
              )}
            </>
          )}
        </Async>
      </Card>
    </div>
  );
}

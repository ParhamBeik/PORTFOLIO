import { useState } from "react";
import { Link } from "react-router-dom";
import AddTransactionDialog from "../components/AddTransactionDialog.jsx";
import LiabilitiesCard from "../components/Liabilities.jsx";
import CorporateActionsCard from "../components/CorporateActions.jsx";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { useApi } from "../useApi.js";
import {
  valuation,
  snapshots,
  getPerformance,
  accountDataQuality,
  updateHolding,
  removeHolding,
  adminAssetEvidence,
  benchmarks,
} from "../api.js";
import {
  ago,
  area,
  holdingLabel,
  humanize,
  indexPoint,
  isWholeUnit,
  money,
  num,
  pct,
  perfLabel,
  perSqm,
  PERF_UNLOCK_HINT,
  quantity,
  signedToman,
  toman,
  unitPrice,
} from "../format.js";
import {
  AreaTrend,
  Donut,
  MultiLineTrend,
} from "../components/charts.jsx";
import {
  Card,
  StatTile,
  Badge,
  Button,
  Delta,
  Tabs,
  Table,
  Empty,
  ErrorState,
  Async,
  Disclosure,
  PageHeader,
  toneFor,
} from "../components/ui.jsx";

const RANGES = [
  { value: "30", label: "30d" },
  { value: "90", label: "90d" },
  { value: "365", label: "1y" },
  { value: "all", label: "All" },
];

const INFLATION_VIEWS = [
  { value: "nominal", label: "Nominal" },
  { value: "real", label: "vs inflation" },
  { value: "benchmarks", label: "vs coin, USD & TEDPIX" },
];

// ponytail: the CPI table is `base 1398=100` (config/settings.py), and a rebase
// is a once-a-decade SCI event, so the base year is a literal here rather than a
// new field on every valuation payload. If it ever moves, serve it from the API.
// Without it "Real Toman" was a number with no unit: 33.6bn nominal showed as
// 1.55bn with nothing on screen saying which year's money that is.
const REAL_BASIS_BASE_YEAR = "1398";
const REAL_BASIS_NOTE = `In constant ${REAL_BASIS_BASE_YEAR} Tomans`;
const BASIS_LABEL = {
  nominal_toman: "Nominal Toman",
  real_toman: `Constant ${REAL_BASIS_BASE_YEAR} Toman`,
  usd_denominated: "US Dollar",
  usdt_denominated: "Tether (USDT)",
};

// What this card measures, said out loud when it has nothing to show.
//
// It reports the return on YOUR money -- cash-flow-boundary TWR and investor
// XIRR -- which needs a tracked opening baseline and enough elapsed time. The
// price-based returns on My Optimal and Comparison need neither, so those pages
// happily printed a 1-year return and a 90-day comparison while this one said
// "available after 71 more days", and the three read as a contradiction.
function PerformanceUnavailable({ detail }) {
  return (
    <Empty testId="dashboard-performance-empty">
      <span>{detail || PERF_UNLOCK_HINT}</span>
      <span className="mt-2 block text-xs text-muted">
        This is the return on the money you put in, which needs a tracked opening
        balance. Price-based returns for the same holdings are already available
        on <Link to="/optimal" className="underline hover:text-text">My Optimal</Link>{" "}
        and <Link to="/comparison" className="underline hover:text-text">Comparison</Link>.
      </span>
    </Empty>
  );
}

const ITEM_BADGE = { live: "good", manual: "warn", stale: "warn", quota: "serious", fallback: "serious", unavailable: "critical" };

// Groups valuation items by asset class for the donut. Palette has 8 fixed
// slots (charts.jsx SERIES), so anything past the top 7 folds into "Other"
// rather than cycling colors and losing legend meaning.
function groupByClass(items) {
  const totals = new Map();
  for (const it of items) {
    const key = it.class || "other";
    totals.set(key, (totals.get(key) || 0) + Number(it.value || 0));
  }
  const groups = [...totals.entries()]
    .map(([name, value]) => ({ name: humanize(name), value }))
    .sort((a, b) => b.value - a.value);
  if (groups.length <= 8) return groups;
  const rest = groups.slice(7).reduce((s, g) => s + g.value, 0);
  return [...groups.slice(0, 7), { name: "Other", value: rest }];
}

/**
 * One chip per market the portfolio holds: open or closed, and how old its
 * newest price is. The total blends a stock market that closed at 12:30 with
 * dollars and coins that are still moving, and a flat stock line beside a
 * live total otherwise reads as a frozen feed. The row keeps its height while
 * loading so nothing below it moves.
 */
function MarketClocks({ data }) {
  const markets = data?.markets || [];
  return (
    <div className="flex min-h-[28px] flex-wrap gap-2" data-testid="dashboard-market-clocks">
      {markets.map((m) => {
        const age = m.last_priced_at ? (Date.now() - Date.parse(m.last_priced_at)) / 1000 : null;
        return (
          <span
            key={m.market}
            data-testid={`dashboard-market-${m.market}`}
            className="inline-flex items-center gap-1.5 rounded-full border border-border bg-panel-2 px-2.5 py-1 text-xs text-muted"
          >
            <span
              aria-hidden="true"
              className={`size-1.5 rounded-full ${m.open ? "bg-[var(--c-good)]" : "bg-[var(--c-muted)]"}`}
            />
            <span className="font-medium text-text">{m.label}</span>
            <span>{m.open ? "open" : "closed"}</span>
            {age != null && <span>· last price {ago(age)}</span>}
          </span>
        );
      })}
    </div>
  );
}

function HeroRow({ state, basis: selected }) {
  return (
    // The hero is the first thing under the page title, so anything below it
    // moves when it grows -- the dashboard's whole measured layout shift was
    // the chart grid being pushed down when the totals replaced the spinner.
    // Reserved responsively rather than through `Async`'s inline `minHeight`,
    // because the two StatTiles stack under `sm` and the reserved height has to
    // stack with them: 184px measured at 412px wide, 94px at 1350px.
    <div className="min-h-[184px] sm:min-h-[94px]">
      <Async {...state} testId="dashboard-hero">
      {(data) => {
        // The basis the NUMBERS were fetched with, not the one the picker shows.
        // `useApi` keeps the previous payload on screen while the next loads, so
        // reading the picker rendered a Toman total under a dollar sign for the
        // length of the request -- $33,600,000,000 for a portfolio worth $167k.
        const basis = data.basis || selected;
        // Under a USD/USDT basis the total IS the dollar figure, so repeating it
        // as an "equivalent" is noise. Under real Toman it is worse than noise:
        // deflated Tomans divided by today's nominal rate is not a dollar amount
        // anyone holds, and it read $7,732 for a portfolio worth $167,578.
        const showUsd =
          basis === "nominal_toman" &&
          data.total_usd !== undefined &&
          data.total_usd !== null;
        return (
          // The "priced holdings N/N" tile was removed: it read "14/14 Manual"
          // on a fully priced book, and the pricing story is already told where
          // it is actionable -- per row in the Status column, and in aggregate
          // by `dashboard-stale-banner` when half the book is not live.
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3" data-testid="dashboard-hero">
            <div className="sm:col-span-2">
              <StatTile
                label="Total value"
                value={money(Number(data.total), basis)}
                size="lg"
                sub={
                  basis === "real_toman"
                    ? REAL_BASIS_NOTE
                    // Cash is part of the total but has no holding row, so say
                    // so rather than leave a gap between the rows and the sum.
                    : Number(data.cash_tomans)
                      ? `Includes ${money(Number(data.cash_tomans), basis)} cash`
                      : undefined
                }
                testId="dashboard-total"
              />
            </div>
            <StatTile
              label={showUsd ? "USD equivalent" : "Valued in"}
              value={showUsd ? "$" + num(Number(data.total_usd)) : BASIS_LABEL[basis]}
              testId="dashboard-usd"
            />
          </div>
        );
      }}
      </Async>
    </div>
  );
}


/**
 * The rate this chart divided by, and where that rate came from.
 *
 * "After inflation" is a claim about a number the reader could not see: two
 * lines diverged and the size of the gap had to be taken on faith. A projected
 * index running at triple the published pace draws the same picture as one
 * running at the right pace, and for a while that is exactly what shipped — the
 * default projection was 6.5%/month (+112%/year) against a verified series that
 * has never left the 31-46%/year band. Printing the applied rate is what makes
 * that visible from the page instead of from the settings file.
 */
function InflationNote({ realGrowth, nominalGrowth, cpi, basis }) {
  if (realGrowth == null) return null;
  const rate = cpi?.applied_annual_rate;
  const estimated = (cpi?.estimated_jalali_years || []).length > 0;
  // Both lines stay in Tomans even when the rest of the page is priced in
  // dollars, because the CPI series measures the Toman. Left unsaid, the chart
  // looks like it ignored the basis selector.
  const foreignBasis = basis !== "nominal_toman" && basis !== "real_toman";
  return (
    <div className="mt-2 space-y-1 text-xs text-muted" data-testid="dashboard-trend-real-note">
      <p>
        {nominalGrowth != null && (
          <>Nominal {nominalGrowth >= 0 ? "+" : "−"}{pct(Math.abs(nominalGrowth))} over this
          window{rate != null ? ", " : ". "}</>
        )}
        {rate != null && (
          <>{nominalGrowth == null ? "Prices" : "prices"} rose {pct(rate)} a year
          over the same days, so </>
        )}
        {/* The rate clause is the only one that hands over mid-sentence, on
            "so ". Every other path ends in a full stop or prints nothing, and
            both of those left a lowercase "in" starting the sentence. */}
        {rate != null ? "in" : "In"} constant Tomans your net worth is{" "}
        {realGrowth >= 0 ? "up" : "down"}{" "}
        {pct(Math.abs(realGrowth))}. The gap between the two lines is inflation,
        not performance.
        {foreignBasis && (
          <> Both lines are shown in Tomans — an inflation comparison only means
          something in the currency being inflated.</>
        )}
      </p>
      {cpi?.source && (
        <p data-testid="dashboard-trend-cpi-source">
          Inflation index: {cpi.source}.
          {estimated && " The current year has no published figure yet, so that part of the line is a projection."}
        </p>
      )}
    </div>
  );
}

function TrendCard({ activeId, basis }) {
  const [range, setRange] = useState("30");
  const [mode, setMode] = useState("nominal");
  const effectiveRange = RANGES.some((r) => r.value === range) ? range : RANGES[0].value;
  const days = effectiveRange === "all" ? "all" : Number(effectiveRange);
  // "After inflation" is a Toman question -- the other line is fetched as
  // `real_toman` and cannot be anything else. Leaving the nominal line on the
  // user's chosen basis drew a dollar series against a constant-Toman one on a
  // single axis: the dollar line flattens onto zero and the Toman axis labels
  // it. In that mode both lines are asked for in Toman and the caption says so.
  const trendBasis = mode === "real" ? "nominal_toman" : basis;
  const state = useApi(
    () => snapshots(days, activeId, trendBasis),
    [days, activeId, trendBasis]
  );
  // The same net worth measured in constant Tomans. Fetched only when asked,
  // because it needs a CPI figure for every Jalali year the window spans and
  // fails loudly rather than silently reusing last year's index.
  const realState = useApi(
    () => snapshots(days, activeId, "real_toman"),
    [days, activeId],
    { enabled: mode === "real" }
  );
  const benchState = useApi(
    () => benchmarks(activeId, { window: effectiveRange === "all" ? "all" : Number(effectiveRange) }),
    [activeId, effectiveRange],
    { enabled: mode === "benchmarks" }
  );
  const trendState = mode === "benchmarks" ? benchState : state;

  return (
    <Card
      title="Net worth"
      testId="dashboard-trend"
      actions={(
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <Tabs
            options={INFLATION_VIEWS}
            value={mode}
            onChange={setMode}
            label="Comparison"
            testId="dashboard-trend-basis"
          />
          <Tabs
            options={RANGES}
            value={effectiveRange}
            onChange={setRange}
            label="Range"
            testId="dashboard-trend-tabs"
          />
        </div>
      )}
    >
      {/* Measured, not guessed: the loaded body is the 260px chart PLUS the
          estimated-points note under it, and reserving only the chart let the
          card grow by ~170px when the data landed — which was the dashboard's
          entire measured layout shift. Floors the transient states only. */}
      <Async {...trendState} testId="dashboard-trend-body" empty="No history yet." minHeight={430}>
        {(data) => {
          if (mode === "benchmarks") {
            const bench = data;
            if (!bench.series?.length) return <Empty>No history yet.</Empty>;
            const keys = Object.keys(bench.labels || {});
            const longTicks = effectiveRange === "365" || effectiveRange === "all";
            const requestedDays =
              effectiveRange === "all" ? bench.requested_window_days : Number(effectiveRange);
            const actualDays = bench.data_window?.observations;
            const shortfall =
              requestedDays &&
              actualDays &&
              requestedDays > actualDays &&
              requestedDays - actualDays > 7;
            return (
              <>
                <MultiLineTrend
                  series={keys.map((k) => ({ key: k, name: bench.labels[k] }))}
                  data={bench.series}
                  longTicks={longTicks}
                  formatValue={indexPoint}
                  formatAxis={indexPoint}
                  label="Your portfolio against gold, the dollar and the TSE index, indexed to 100"
                />
                <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-bench-note">
                  Each line starts at 100, so the gap is relative growth over the
                  window — not the amount of money in each.
                </p>
                {shortfall && (
                  <p className="mt-1 text-xs text-muted" data-testid="dashboard-trend-bench-shortfall">
                    You asked for {requestedDays} days and this covers {actualDays} — that is as
                    much shared history as these series have in common.
                  </p>
                )}
                {(bench.unavailable || []).map((u) => (
                  <p key={u.key} className="mt-1 text-xs text-muted">
                    {u.label} not shown: {u.reason}.
                  </p>
                ))}
              </>
            );
          }

          const points = (data.series || []).map((s) => ({ x: s.date, y: Number(s.total) }));
          // Counted, not just detected. "Some points are estimated" reads like a
          // footnote when 47 of 66 points are reconstructed rather than recorded,
          // which is a different chart from the one that phrasing implies.
          const estimatedCount = (data.series || []).filter((s) => s.is_estimated).length;
          const pointCount = (data.series || []).length;
          const hasEstimated = estimatedCount > 0;
          // Set when a switched-off holding had no recorded close for that day and
          // its current price stood in while netting it out of the history.
          const hasApproximated = (data.series || []).some((s) => s.approximated);
          const longTicks = effectiveRange === "365" || effectiveRange === "all";

          if (mode === "real" && realState.data?.series?.length) {
            const real = new Map(
              realState.data.series.map((s) => [s.date, Number(s.total)])
            );
            const merged = points.map((p) => ({ x: p.x, nominal: p.y, real: real.get(p.x) ?? null }));
            const first = merged.find((m) => m.real != null);
            const last = [...merged].reverse().find((m) => m.real != null);
            const realGrowth = first && last && first.real ? last.real / first.real - 1 : null;
            // Measured between the SAME two days as the real figure, so the two
            // are subtractable. Reading the nominal ends off `points` instead
            // would compare a longer span against a shorter one whenever the
            // real series is missing a day at either end.
            const nominalGrowth =
              first && last && first.nominal ? last.nominal / first.nominal - 1 : null;
            return (
              <>
                <MultiLineTrend
                  series={[
                    { key: "nominal", name: "Nominal" },
                    { key: "real", name: "After inflation" },
                  ]}
                  data={merged}
                  longTicks={longTicks}
                  label="Net worth, nominal versus after inflation"
                />
                <InflationNote
                  realGrowth={realGrowth}
                  nominalGrowth={nominalGrowth}
                  cpi={realState.data.cpi}
                  basis={basis}
                />
              </>
            );
          }

          return (
            <>
              {/* The basis the POINTS are in, not the one the picker shows: the
                  previous series stays on screen while the next one loads, and
                  the server answers `nominal_toman` when it had no rate to
                  convert by. Reading it off the data keeps the axis, the tooltip
                  and the numbers describing the same currency. */}
              <AreaTrend
                data={points}
                longTicks={longTicks}
                basis={data.basis || basis}
              />
              {mode === "real" && realState.error && (
                <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-real-error">
                  No inflation-adjusted series for this window: {realState.error.message}
                </p>
              )}
              {hasEstimated && (
                <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-note">
                  {estimatedCount} of {pointCount} points are rebuilt from prices
                  because no daily snapshot was recorded for those days.
                </p>
              )}
              {hasApproximated && (
                <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-hidden-note">
                  On some days an asset you switched off had no recorded price, so
                  the amount removed from the line there is an estimate.
                </p>
              )}
            </>
          );
        }}
      </Async>
    </Card>
  );
}

function AllocationCard({ state }) {
  return (
    <Card title="Allocation" testId="dashboard-allocation">
      {/* Loaded body measures 382px: the 260px donut plus its legend column. */}
      <Async {...state} testId="dashboard-allocation-body" empty="No priced holdings yet." minHeight={385}>
        {(data) => {
          const groups = groupByClass(data.items || []);
          if (!groups.length) return <Empty>No priced holdings yet.</Empty>;
          // `Donut` defaults its tooltip to Toman, so this was the last panel on
          // the page still suffixing a converted dollar amount " T" after the
          // hero, the rows and the trend chart were relabelled.
          return (
            <Donut
              data={groups}
              testId="dashboard-donut"
              valueFormat={(v) => money(v, data.basis)}
            />
          );
        }}
      </Async>
    </Card>
  );
}

// Performance is account-scoped on the API. When the top bar is on "All
// portfolios", use the sole account or every ledger-complete account.
function performanceTargets(activeId, accounts) {
  if (activeId != null) return { mode: "single", ids: [activeId] };
  if (accounts.length === 1) return { mode: "single", ids: [accounts[0].id] };
  const ready = accounts.filter((a) => a.ledger_complete).map((a) => a.id);
  if (ready.length) return { mode: "multi", ids: ready };
  if (accounts.length) return { mode: "single", ids: [accounts[0].id] };
  return { mode: "none", ids: [] };
}

async function fetchPerformance(targets, accounts, basis) {
  if (targets.mode === "multi") {
    const rows = await Promise.all(
      targets.ids.map(async (id) => {
        const perf = await getPerformance(id, basis);
        const acct = accounts.find((a) => a.id === id);
        return { id, name: acct?.name || `Account ${id}`, ...perf };
      })
    );
    return { aggregate: true, accounts: rows };
  }
  return getPerformance(targets.ids[0], basis);
}

function PerformanceMetrics({ data }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:max-w-md">
      <StatTile label={perfLabel.twr} value={pct(data.twr)} valueTone={toneFor(data.twr)} testId="dashboard-performance-twr" />
      <StatTile label={perfLabel.xirr} value={pct(data.xirr)} valueTone={toneFor(data.xirr)} testId="dashboard-performance-xirr" />
    </div>
  );
}

/** `assets` map -> rows, plus the account name when several are merged. */
function positionRows(assets, accountName) {
  return Object.entries(assets || {}).map(([key, v]) => ({
    key: accountName ? `${accountName}:${key}` : key,
    account_name: accountName,
    ...v,
  }));
}

/**
 * What you paid, and what it is worth now.
 *
 * Deliberately separate from TWR/XIRR: those are annualized and are noise
 * before 90 tracked days, but cost basis and P&L are neither annualized nor
 * time-weighted -- they are the recorded trades against today's price, correct
 * from the first buy. Rendering them only in the unlocked branch left this
 * panel blank for the first three months of an account's life, which is
 * exactly when someone most wants to know what they paid.
 */
function PositionsTable({ rows, showAccount }) {
  const columns = [
    { key: "asset", header: "Asset", render: (r) => r.asset_name },
    {
      key: "qty",
      header: "Quantity",
      align: "right",
      // A property is held by the square meter, and its "quantity" is an area.
      // Printed through the count formatter it read as 100 of something.
      render: (r) =>
        r.quantity_unit === "sqm"
          ? area(r.quantity)
          : quantity(r.quantity, r.quantity_step),
    },
    // Named in the currency it was paid in. A stock's average cost is a Rial
    // figure sitting one column away from Toman cost basis; an unlabelled
    // number there reads as ten times what was paid. A property's is per
    // square meter, which is a different unit again — 24m/m² beside 24m flat
    // is a four-order-of-magnitude difference with nothing on screen saying so.
    {
      key: "avg",
      header: "Avg cost",
      align: "right",
      render: (r) =>
        r.average_cost_unit === "sqm"
          ? perSqm(r.average_cost_tomans)
          : unitPrice(r.average_cost_tomans, r.average_cost_currency),
    },
    { key: "basis", header: "Cost basis", align: "right", render: (r) => r.total_cost_basis_tomans == null ? "—" : toman(r.total_cost_basis_tomans) },
    { key: "value", header: "Current value", align: "right", render: (r) => r.current_value_tomans == null ? "—" : toman(r.current_value_tomans) },
    { key: "realized", header: "Realized P&L", align: "right", render: (r) => r.realized_pnl_tomans == null ? "—" : <Delta value={r.realized_pnl_tomans} format={signedToman} /> },
    { key: "unrealized", header: "Unrealized P&L", align: "right", render: (r) => r.unrealized_pnl_tomans == null ? "—" : <Delta value={r.unrealized_pnl_tomans} format={signedToman} /> },
  ];
  if (showAccount) {
    columns.splice(1, 0, { key: "portfolio", header: "Portfolio", render: (r) => r.account_name || "—" });
  }
  return (
    <Table
      testId="dashboard-performance-table"
      caption="Position performance"
      mobileCards
      rowKey={(r) => r.key}
      rows={rows}
      columns={columns}
    />
  );
}

/** The two annualized numbers are still locked; say so above the table. */
function PerformanceLockedNote({ detail }) {
  return (
    <p className="mb-3 text-xs text-muted" data-testid="dashboard-performance-locked-note">
      {detail || PERF_UNLOCK_HINT} Until then, what you paid and what it is worth
      now are shown below — those need no tracking history. Price-based returns
      are on <Link to="/optimal" className="underline hover:text-text">My Optimal</Link>{" "}
      and <Link to="/comparison" className="underline hover:text-text">Comparison</Link>.
    </p>
  );
}

function PerformanceCard({ activeId, basis, accounts }) {
  const targets = performanceTargets(activeId, accounts);
  const accountKey = accounts.map((a) => `${a.id}:${a.ledger_complete}`).join("|");
  const state = useApi(
    () => fetchPerformance(targets, accounts, basis),
    [activeId, basis, accountKey, targets.ids.join(",")],
    { enabled: targets.ids.length > 0 }
  );

  if (targets.ids.length === 0) {
    return (
      <Card title="Performance" testId="dashboard-performance">
        <Empty testId="dashboard-performance-empty">Add a portfolio to track performance.</Empty>
      </Card>
    );
  }

  return (
    <Card title="Performance" testId="dashboard-performance">
      <Async {...state} testId="dashboard-performance-body">
        {(data) => {
          if (data.aggregate) {
            const ready = data.accounts.filter((row) => row.performance_available);
            if (!ready.length) {
              // Same rule as the single-account branch: the returns are locked,
              // the positions are not. Rows are tagged with their portfolio,
              // because two accounts can hold the same asset at different costs.
              const positions = data.accounts.flatMap((row) =>
                positionRows(row.assets, row.name)
              );
              if (!positions.length) {
                return <PerformanceUnavailable detail={data.accounts[0]?.detail} />;
              }
              return (
                <>
                  <PerformanceLockedNote detail={data.accounts[0]?.detail} />
                  <PositionsTable rows={positions} showAccount />
                </>
              );
            }
            return (
              <>
                {activeId == null && accounts.length > 1 && (
                  <p className="mb-3 text-xs text-muted">All portfolios with a completed opening baseline.</p>
                )}
                <Table
                  testId="dashboard-performance-table"
                  caption="Portfolio performance"
                  mobileCards
                  rowKey={(r) => r.id}
                  rows={data.accounts}
                  columns={[
                    { key: "name", header: "Portfolio", render: (r) => r.name },
                    {
                      key: "twr",
                      header: perfLabel.twr,
                      align: "right",
                      render: (r) => (r.performance_available ? pct(r.twr) : "—"),
                    },
                    {
                      key: "xirr",
                      header: perfLabel.xirr,
                      align: "right",
                      render: (r) => (r.performance_available ? pct(r.xirr) : "—"),
                    },
                    {
                      key: "status",
                      header: "Status",
                      render: (r) =>
                        r.performance_available ? (
                          <Badge variant="good">Ready</Badge>
                        ) : (
                          <span className="text-xs text-muted">{r.detail || "Needs ledger"}</span>
                        ),
                    },
                  ]}
                />
              </>
            );
          }
          const rows = positionRows(data.assets);
          if (!data.performance_available) {
            if (!rows.length) return <PerformanceUnavailable detail={data.detail} />;
            return (
              <>
                <PerformanceLockedNote detail={data.detail} />
                <PositionsTable rows={rows} />
              </>
            );
          }
          return (
            <>
              <PerformanceMetrics data={data} />
              <div className="mt-4">
                <PositionsTable rows={rows} />
              </div>
            </>
          );
        }}
      </Async>
    </Card>
  );
}

function holdingsByAccountAsset(accounts) {
  const map = new Map();
  for (const account of accounts) {
    for (const holding of account.holdings || []) {
      map.set(`${account.id}:${holding.asset_key}`, holding);
    }
  }
  return map;
}

function holdingsRowKey(row) {
  return row.account_id != null ? `${row.account_id}:${row.key}` : row.key;
}

function sortHoldingsByPortfolioValue(items) {
  const portfolioOrder = [];
  const grouped = new Map();
  for (const item of items) {
    const portfolioKey = item.account_id == null ? "__single__" : String(item.account_id);
    if (!grouped.has(portfolioKey)) {
      grouped.set(portfolioKey, []);
      portfolioOrder.push(portfolioKey);
    }
    grouped.get(portfolioKey).push(item);
  }
  return portfolioOrder.flatMap((portfolioKey) =>
    grouped.get(portfolioKey).sort((a, b) => {
      const valueDelta = Number(b.value || 0) - Number(a.value || 0);
      return valueDelta || holdingLabel(a).localeCompare(holdingLabel(b));
    })
  );
}

function isManualPriceEditable(row) {
  return !row.is_house && (
    row.is_manual || row.quality_status === "manual" || row.source === "manual_valuation"
  );
}

/**
 * The stored decimal written as the shortest string meaning the same number.
 *
 * The API serializes Decimals verbatim, so four coins arrive as "4.000000" and
 * seeded the editor with six meaningless decimals on a row whose spinner steps
 * by one — the box contradicted the column beside it, which printed "4".
 * Trimming is string-level on purpose: sending a large Toman price through
 * Number() to tidy it is how precision gets lost.
 */
const trimZeros = (v) => {
  const s = String(v);
  return s.includes(".") ? s.replace(/\.?0+$/, "") : s;
};

// For a property `qty` is the price per square meter in millions of Toman — the
// column the API stores it in — and `area` is its size. Those are the two numbers
// a property is described by; the raw `quantity` is never shown on its own.
function originalDraft(row) {
  return {
    qty: row.quantity == null ? "" : trimZeros(row.quantity),
    price: row.unit_price != null && row.unit_price !== "" ? trimZeros(row.unit_price) : "",
    area: row.area_sqm != null ? trimZeros(row.area_sqm) : "",
  };
}

function draftForRow(row, drafts) {
  return drafts[holdingsRowKey(row)] ?? originalDraft(row);
}

function hasDraftChanges(row, draft) {
  // Compared against the same trimmed strings the editor was seeded with, or
  // opening the editor would look like an unsaved change on every row.
  const { qty: origQty, price: origPrice, area: origArea } = originalDraft(row);
  if (draft.qty.trim() !== origQty) return true;
  if (row.is_house) return draft.area.trim() !== origArea;
  return isManualPriceEditable(row) && draft.price.trim() !== origPrice;
}

// Every holding is renamable. `display_name` lives on the HOLDING, not on the
// shared Asset (see models.Holding.display_name), so naming your copy "Dad's
// gold" or "کاما - بلندمدت" cannot rename anything for another user. Restricting
// it to house/manual rows was a guess at where nicknames were wanted, and it
// left most of the book with a name the owner could not change.

const inlineInputClass = "w-full min-w-[5rem] rounded-md border border-border bg-panel px-2 py-1 text-right text-sm tabular";

function PricingGlossaryDisclosure() {
  return (
    <Disclosure summary="What do Live / Manual / Stale / Quota mean?" testId="dashboard-pricing-glossary">
      <ul className="space-y-1">
        <li><strong className="text-text">Live</strong> — priced within the last 5 minutes, or the last print from the latest session while that market is shut.</li>
        <li><strong className="text-text">Manual</strong> — house or real-estate marks updated within the last 90 days.</li>
        <li><strong className="text-text">Stale</strong> — the quote is from a previous session; a fresher one should have arrived.</li>
        <li><strong className="text-text">Quota</strong> — the provider refused further requests today; showing the last known price, which is not current.</li>
        <li><strong className="text-text">Mixed</strong> — some holdings are stale, quota-blocked, or falling back to an archived price.</li>
        <li><strong className="text-text">Real Toman</strong> — inflation-adjusted using the Statistical Center of Iran's published CPI. The year in progress has no release yet, so that stretch is a labelled projection; the "vs inflation" view prints the rate it used.</li>
      </ul>
    </Disclosure>
  );
}

function HoldingsCard({ activeId, valuationState, portfolio, admin }) {
  const [manageMode, setManageMode] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [savingKey, setSavingKey] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [whyKey, setWhyKey] = useState(null);
  const [adding, setAdding] = useState(false);

  const reloadAll = () => {
    valuationState.reload();
    portfolio.reload();
  };

  const holdingsMap = holdingsByAccountAsset(portfolio.accounts);
  // Flat holding rows with their owning account attached — what the add dialog
  // needs to revalue a property it already holds.
  const allHoldings = portfolio.accounts.flatMap((a) =>
    (a.holdings || []).map((h) => ({ ...h, account_id: a.id }))
  );

  const resolveHolding = (row) => {
    const accountId = row.account_id ?? activeId;
    if (accountId == null) return null;
    return holdingsMap.get(`${accountId}:${row.key}`) ?? null;
  };

  const toggleManageMode = (mode) => {
    // The next mode is derived here rather than inside a `setManageMode`
    // updater. React runs an updater *during render* -- twice under
    // StrictMode -- so calling `portfolio.reload()` from inside one set state
    // on PortfolioProvider mid-render (React's "Cannot update a component while
    // rendering a different component") and issued the accounts request twice
    // on every click of Edit.
    const next = manageMode === mode ? null : mode;
    setManageMode(next);
    if (next === "edit") portfolio.reload();
    setDrafts({});
    setActionError(null);
  };

  const setDraftField = (row, field, value) => {
    const key = holdingsRowKey(row);
    setDrafts((cur) => ({
      ...cur,
      [key]: { ...draftForRow(row, cur), [field]: value },
    }));
  };

  const saveInline = async (row, holding) => {
    const accountId = row.account_id ?? activeId;
    if (!holding || accountId == null) return;
    const draft = draftForRow(row, drafts);
    const qty = draft.qty.trim();
    if (!qty) return;
    if (!hasDraftChanges(row, draft)) return;
    // A counted asset typed as a fraction is refused, not rounded. The spinner
    // already steps by one; someone who types 3.5 shares means something, and
    // quietly saving 3 or 4 of them is the kind of silent correction that makes
    // a portfolio stop matching the brokerage statement it came from.
    if (!row.is_house && isWholeUnit(row.quantity_step) && !Number.isInteger(Number(qty))) {
      setActionError(
        new Error(
          `${holdingLabel(row)} is counted in whole units — enter a whole number.`
        )
      );
      return;
    }
    if (!row.is_house && Number(qty) === 0 && !window.confirm(
      `Sell all of ${holdingLabel(row)}? This records a sale in your ledger and removes it from holdings.`
    )) {
      return;
    }
    const key = holdingsRowKey(row);
    setSavingKey(key);
    setActionError(null);
    try {
      await updateHolding(accountId, holding.id, {
        quantity: qty,
        confirmSellAll: !row.is_house && Number(qty) === 0,
        areaSqm: row.is_house ? draft.area : undefined,
        unitPriceTomans: isManualPriceEditable(row) ? draft.price : undefined,
      });
      setDrafts((cur) => {
        const next = { ...cur };
        delete next[key];
        return next;
      });
      reloadAll();
    } catch (e) {
      setActionError(e);
    } finally {
      setSavingKey(null);
    }
  };

  /** Rename and visibility are saved on their own, the moment they change. */
  const patchPresentation = async (row, holding, body) => {
    const accountId = row.account_id ?? activeId;
    if (!holding || accountId == null) return;
    const key = holdingsRowKey(row);
    setSavingKey(key);
    setActionError(null);
    try {
      await updateHolding(accountId, holding.id, body);
      reloadAll();
    } catch (e) {
      setActionError(e);
    } finally {
      setSavingKey(null);
    }
  };

  const handleDelete = async (holding, accountId) => {
    if (!window.confirm("Remove this holding?")) return;
    setActionError(null);
    try {
      await removeHolding(accountId, holding.id);
      reloadAll();
    } catch (e) {
      setActionError(e);
    }
  };

  const cardActions = (
    <div className="flex gap-2">
      <Button
        variant={manageMode === "edit" ? "primary" : "ghost"}
        onClick={() => toggleManageMode("edit")}
        data-testid="dashboard-holdings-manage-edit"
      >
        Edit
      </Button>
      <Button
        variant={manageMode === "delete" ? "danger" : "ghost"}
        onClick={() => toggleManageMode("delete")}
        data-testid="dashboard-holdings-manage-delete"
      >
        Delete
      </Button>
    </div>
  );

  return (
    <Card title="Holdings" testId="dashboard-holdings" actions={cardActions}>
      <Async {...valuationState} testId="dashboard-holdings-body">
        {(data) => {
          // The basis these rows were priced in, not the one the picker shows:
          // the previous payload stays on screen while the next loads.
          const rowBasis = data.basis || portfolio.basis;
          const visible = data.items || [];
          const hidden = data.hidden_items || [];
          // Switched-off rows stay on screen, dimmed, so the user can see what
          // they are leaving out and switch it back on. They are excluded from
          // the weight base for the same reason they are excluded from the
          // total: they are not part of the portfolio being measured.
          const items = sortHoldingsByPortfolioValue([...visible, ...hidden]);
          // `data.total` is the NET figure (liabilities and real estate netted
          // off), while these rows are gross holding values. Dividing by it gave
          // a 945M holding a 109.7% weight. Weight is a share of what is listed.
          const weightBase =
            visible.reduce((sum, i) => sum + Number(i.value || 0), 0) || 1;
          const staleCount = visible.filter((i) => i.quality_status && i.quality_status !== "live").length;
          const showStaleBanner = visible.length > 0 && staleCount / visible.length >= 0.5;

          const columns = [
            {
              key: "include",
              header: "",
              render: (r) => {
                const holding = resolveHolding(r);
                if (!holding) return null;
                return (
                  <input
                    type="checkbox"
                    checked={!r.is_hidden}
                    disabled={savingKey === holdingsRowKey(r)}
                    aria-label={`Count ${holdingLabel(r)} in this portfolio`}
                    title={
                      r.is_hidden
                        ? "Switched off — not counted anywhere. Tick to include it again."
                        : "Counted. Untick to leave it out of every figure without deleting it."
                    }
                    data-testid="dashboard-holdings-include"
                    onChange={(e) =>
                      patchPresentation(r, holding, { isHidden: !e.target.checked })
                    }
                  />
                );
              },
            },
            {
              key: "asset",
              header: "Asset",
              render: (r) => {
                const holding = resolveHolding(r);
                if (manageMode === "edit" && holding) {
                  return (
                    <input
                      className={`${inlineInputClass} text-left`}
                      defaultValue={r.display_name || ""}
                      // The name it falls back to when cleared — the ticker for
                      // a stock, the catalog name otherwise.
                      placeholder={r.symbol || r.name_fa || r.asset}
                      aria-label={`Name for ${holdingLabel(r)}`}
                      data-testid="dashboard-holdings-edit-name"
                      disabled={savingKey === holdingsRowKey(r)}
                      onBlur={(e) => {
                        const next = e.target.value.trim();
                        if (next !== (r.display_name || "")) {
                          patchPresentation(r, holding, { displayName: next });
                        }
                      }}
                    />
                  );
                }
                // A stock reads as its ticker (`Holding.label`); the registered
                // company name is long enough to break the row and is not how
                // anyone refers to it, so it lives in the tooltip.
                return <span title={r.name_fa || r.asset}>{holdingLabel(r)}</span>;
              },
            },
            { key: "class", header: "Class", render: (r) => humanize(r.class) },
            {
              key: "qty",
              header: "Size / quantity",
              align: "right",
              render: (r) => {
                const holding = resolveHolding(r);
                const editing = manageMode === "edit" && holding;
                const draft = draftForRow(r, drafts);
                const rk = holdingsRowKey(r);
                // A property's stored "quantity" is its price per square meter,
                // so what belongs in this column is its size, not that number.
                if (r.is_house) {
                  return editing ? (
                    <input
                      type="number"
                      step="any"
                      className={inlineInputClass}
                      value={draft.area}
                      aria-label={`Size in square meters for ${holdingLabel(r)}`}
                      data-testid="dashboard-holdings-edit-area"
                      disabled={savingKey === rk}
                      onChange={(e) => setDraftField(r, "area", e.target.value)}
                    />
                  ) : (
                    area(r.area_sqm)
                  );
                }
                if (editing) {
                  // `step` comes from the server, so the arrow keys and the
                  // spinner move by one share / one coin / one note instead of
                  // offering a fraction of something you cannot hold a fraction
                  // of. Crypto, gold by the gram and tether keep a fine step.
                  const step = r.quantity_step || "any";
                  return (
                    <input
                      type="number"
                      step={step}
                      min="0"
                      className={inlineInputClass}
                      value={draft.qty}
                      aria-label={`Quantity for ${holdingLabel(r)}`}
                      title={
                        isWholeUnit(step)
                          ? "Counted in whole units"
                          : "This asset can be held in fractions"
                      }
                      data-testid="dashboard-holdings-edit-qty"
                      disabled={savingKey === rk}
                      onChange={(e) => setDraftField(r, "qty", e.target.value)}
                    />
                  );
                }
                return quantity(r.quantity, r.quantity_step);
              },
            },
            {
              key: "price",
              header: "Unit price",
              align: "right",
              render: (r) => {
                const holding = resolveHolding(r);
                const draft = draftForRow(r, drafts);
                const rk = holdingsRowKey(r);
                if (r.is_house) {
                  // Editing a property means editing what a meter is worth --
                  // entered in millions, the unit the market quotes in.
                  return manageMode === "edit" && holding ? (
                    <input
                      type="number"
                      step="any"
                      className={inlineInputClass}
                      value={draft.qty}
                      aria-label={`Price per square meter, in millions, for ${holdingLabel(r)}`}
                      title="Millions of Toman per square meter"
                      data-testid="dashboard-holdings-edit-price-per-sqm"
                      disabled={savingKey === rk}
                      onChange={(e) => setDraftField(r, "qty", e.target.value)}
                    />
                  ) : (
                    perSqm(r.price_per_sqm_tomans)
                  );
                }
                if (manageMode === "edit" && isManualPriceEditable(r) && holding) {
                  return (
                    <input
                      type="number"
                      step="any"
                      className={inlineInputClass}
                      value={draft.price}
                      aria-label={`Unit price for ${holdingLabel(r)}`}
                      data-testid="dashboard-holdings-edit-price"
                      disabled={savingKey === rk}
                      onChange={(e) => setDraftField(r, "price", e.target.value)}
                    />
                  );
                }
                const shown = unitPrice(r.unit_price, r.unit_price_currency, rowBasis);
                // Say WHY this one is not a box. A market-priced asset takes its
                // quote from the feed, and any number typed here would be
                // overwritten by the next fetch while quietly mispricing the
                // portfolio in the meantime — so the field stays read-only and
                // explains itself instead of looking broken.
                if (manageMode === "edit" && holding) {
                  return (
                    <span
                      className="text-muted"
                      title="Priced from the market feed — not editable. Only manual assets and property take a price you type."
                    >
                      {shown}
                    </span>
                  );
                }
                return shown;
              },
            },
            {
              key: "value",
              header: "Value",
              align: "right",
              render: (r) =>
                r.is_hidden ? (
                  <span className="line-through">{money(r.value, rowBasis)}</span>
                ) : (
                  money(r.value, rowBasis)
                ),
            },
            {
              key: "weight",
              header: "Weight",
              align: "right",
              render: (r) => (r.is_hidden ? "—" : pct(Number(r.value) / weightBase)),
            },
            {
              key: "status",
              header: "Status",
              render: (r) => (
                <div className="flex flex-wrap items-center gap-1">
                  {r.is_hidden && <Badge variant="warn">Not counted</Badge>}
                  <Badge variant={ITEM_BADGE[r.quality_status] || "neutral"}>{humanize(r.quality_status)}</Badge>
                  {r.price_unit_status === "unverified" && <Badge variant="warn">unverified unit</Badge>}
                </div>
              ),
            },
            { key: "source", header: "Source", render: (r) => r.source || "—" },
            { key: "priced_at", header: "As of", render: (r) => r.priced_at ? ago(r.age_seconds) : (r.archive_record?.date || "—") },
          ];

          if (activeId == null) {
            columns.splice(1, 0, {
              key: "portfolio",
              header: "Portfolio",
              render: (r) => r.account_name || "—",
            });
          }

          if (admin) {
            columns.push({
              key: "why",
              header: "",
              render: (r) => (
                <Button variant="ghost" onClick={() => setWhyKey(r.key)} data-testid="dashboard-why">Why</Button>
              ),
            });
          }

          // Save/Delete go FIRST, not last. This table is 12 columns wide and
          // already overflows its card on any normal screen, so a button
          // appended at the end landed hundreds of pixels off the right edge
          // behind a horizontal scrollbar nobody looks for -- which is why
          // clicking Edit or Delete read as "nothing happened". Playwright's
          // toBeVisible() passes on an off-screen cell inside a scroll
          // container, so the e2e suite never caught it either.
          if (manageMode === "edit") {
            columns.unshift({
              key: "actions",
              header: "",
              render: (r) => {
                const holding = resolveHolding(r);
                if (!holding) return <span className="text-xs text-muted">—</span>;
                const draft = draftForRow(r, drafts);
                const rk = holdingsRowKey(r);
                const changed = hasDraftChanges(r, draft);
                return (
                  <Button
                    variant="success"
                    disabled={!changed || !draft.qty.trim() || savingKey === rk}
                    onClick={() => saveInline(r, holding)}
                    data-testid="dashboard-holdings-save"
                  >
                    {savingKey === rk ? "Saving…" : "Save"}
                  </Button>
                );
              },
            });
          }

          if (manageMode === "delete") {
            columns.unshift({
              key: "actions",
              header: "",
              render: (r) => {
                const holding = resolveHolding(r);
                if (!holding) return null;
                const accountId = r.account_id ?? activeId;
                return (
                  <Button variant="danger" onClick={() => handleDelete(holding, accountId)} data-testid="dashboard-holdings-delete">
                    Delete
                  </Button>
                );
              },
            });
          }

          return (
            <>
              {showStaleBanner && (
                <div
                  role="status"
                  data-testid="dashboard-stale-banner"
                  className="mb-3 rounded-lg border border-[var(--c-warn)]/40 bg-[var(--c-warn)]/10 px-4 py-3 text-sm"
                >
                  Live pricing unavailable for {staleCount} of {visible.length} holdings — showing archived or manual prices instead.
                </div>
              )}
              {hidden.length > 0 && (
                <p className="mb-3 text-sm text-muted" data-testid="dashboard-holdings-hidden-note">
                  {hidden.length === 1 ? "One asset is" : `${hidden.length} assets are`} switched
                  off — listed below but left out of your total, your allocation, your risk and
                  your history. Tick the box to count {hidden.length === 1 ? "it" : "them"} again.
                </p>
              )}
              {manageMode === "edit" && (
                <p className="mb-3 text-xs text-muted" data-testid="dashboard-holdings-edit-hint">
                  Rename any holding — the name is yours and saves as soon as you click away.
                  Change a quantity and click Save on the left. A manual asset also takes a unit
                  price, and a property takes its size plus what a square meter is worth, in
                  millions of Toman. Market-priced assets keep the feed's price.
                </p>
              )}
              {manageMode === "delete" && (
                <p className="mb-3 text-xs text-muted" data-testid="dashboard-holdings-delete-hint">
                  Delete removes the holding and reverses its ledger entries. To keep a holding
                  but leave it out of every figure, untick its box instead.
                </p>
              )}
              <Table
                testId="dashboard-holdings-table"
                caption="Portfolio holdings"
                mobileCards
                rowKey={holdingsRowKey}
                rows={items}
                columns={columns}
                empty="No holdings priced yet."
                // `text-muted` rather than `opacity-50`: opacity dims EVERY descendant,
                // including the "Not counted" badge that explains why the row is
                // dimmed -- which measured 3.05:1. Muting the text leaves the badge
                // at full strength, and `--c-muted` is a token the contrast test
                // already guards on every background.
                rowClass={(r) => (r.is_hidden ? "text-muted" : "")}
              />
              <PricingGlossaryDisclosure />
              {actionError && (
                <div className="mt-2">
                  <ErrorState error={actionError} testId="dashboard-holdings-error" />
                </div>
              )}
              <div className="mt-3">
                <Button
                  variant="primary"
                  onClick={() => setAdding(true)}
                  data-testid="dashboard-add-button"
                >
                  Add an asset
                </Button>
              </div>
              {adding && (
                <AddTransactionDialog
                  accountId={activeId}
                  accounts={portfolio.accounts}
                  holdings={allHoldings}
                  onClose={() => setAdding(false)}
                  onSaved={reloadAll}
                />
              )}
              {admin && whyKey && <WhyDrawer assetKey={whyKey} onClose={() => setWhyKey(null)} />}
            </>
          );
        }}
      </Async>
    </Card>
  );
}



function ExcludedDisclosure({ valuationState }) {
  const excluded = valuationState.data?.excluded;
  if (!excluded?.length) return null;
  return (
    <Disclosure summary="Assets excluded from this valuation" testId="dashboard-excluded">
      <ul className="space-y-1">
        {excluded.map((e) => (
          <li key={e.asset_key}>
            {e.asset_key} — {humanize(e.reason)}
          </li>
        ))}
      </ul>
    </Disclosure>
  );
}

function WhyDrawer({ assetKey, onClose }) {
  const state = useApi(() => adminAssetEvidence(assetKey), [assetKey]);
  return (
    <Card
      className="mt-3"
      testId="dashboard-why-drawer"
      title={`Why ${assetKey}`}
      actions={<Button variant="ghost" onClick={onClose}>Close</Button>}
    >
      <Async {...state} testId="dashboard-why-body">
        {(data) => (
          <div className="space-y-2 text-sm">
            {(data.claims || []).map((c) => (
              <p key={c.id} title={c.definition}>
                <Badge variant={c.passed ? "good" : "warn"}>{c.label}: {c.passed ? "yes" : "no"}</Badge>
              </p>
            ))}
            <p className="text-muted">
              Source {data.displayed_value?.source || "—"} · as of {data.displayed_value?.priced_at || data.displayed_value?.archive_record?.date || "—"}
            </p>
            {data.suggested_cli && <p className="text-xs text-muted">{data.suggested_cli}</p>}
          </div>
        )}
      </Async>
    </Card>
  );
}


function HistoryQualityCard({ activeId }) {
  const state = useApi(
    () => accountDataQuality(activeId),
    [activeId],
    { enabled: Boolean(activeId) }
  );
  if (!activeId) {
    return (
      <p className="text-sm text-muted" data-testid="dashboard-quality-all">
        Select one portfolio to see whether its price history is complete enough to trust.
      </p>
    );
  }
  return (
    <Card title="History quality" testId="dashboard-quality">
      <Async {...state} testId="dashboard-quality-body" empty="No history-quality data yet.">
        {(data) => {
          const failing = (data.assets || []).filter((a) => a.passes_gate === false);
          const tone =
            data.quality_status === "complete" ? "good"
            : data.quality_status === "partial" ? "warn"
            : "neutral";
          return (
            <div className="space-y-2 text-sm">
              <p>
                <Badge variant={tone}>{humanize(data.quality_status)}</Badge>
                {" "}
                {data.passing_assets} of {data.assessed_assets} priced holdings pass the integrity gate.
              </p>
              {failing.length > 0 && (
                <ul className="list-disc pl-5 text-muted">
                  {failing.map((a) => (
                    <li key={a.asset_key}>
                      {a.symbol || a.asset_key}
                      {a.reason_codes?.length ? ` — ${a.reason_codes.map(humanize).join(", ")}` : ""}
                    </li>
                  ))}
                </ul>
              )}
              <p className="text-xs text-muted">
                Live quotes on the holdings table are a different question. This card is about the warehouse history behind returns and P/L.
              </p>
            </div>
          );
        }}
      </Async>
    </Card>
  );
}



export default function Dashboard({ user }) {
  const portfolio = usePortfolio();
  const { activeId, basis } = portfolio;
  const valuationState = useApi(() => valuation(activeId, basis), [activeId, basis], { pollMs: 60000 });

  return (
    <div>
      <PageHeader title="Portfolio" subtitle="Your holdings, net worth, allocation, and performance in the selected valuation basis." />
      <div className="space-y-6">
        <HeroRow state={valuationState} basis={basis} />
        <MarketClocks data={valuationState.data} />
        <CorporateActionsCard
          onBooked={() => {
            valuationState.reload();
            portfolio.reload();
          }}
        />
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <TrendCard activeId={activeId} basis={basis} />
          </div>
          <AllocationCard state={valuationState} />
        </div>
        <HoldingsCard activeId={activeId} valuationState={valuationState} portfolio={portfolio} admin={user?.role === "admin"} />
        <HistoryQualityCard activeId={activeId} />
        <LiabilitiesCard activeId={activeId} accounts={portfolio.accounts} />
        <PerformanceCard activeId={activeId} basis={basis} accounts={portfolio.accounts} />
        <ExcludedDisclosure valuationState={valuationState} />
      </div>
    </div>
  );
}

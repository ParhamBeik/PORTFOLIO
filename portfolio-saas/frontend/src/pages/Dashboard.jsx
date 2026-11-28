import { useState } from "react";
import { Link } from "react-router-dom";
import AddTransactionDialog from "../components/AddTransactionDialog.jsx";
import LiabilitiesCard from "../components/Liabilities.jsx";
import CorporateActionsCard from "../components/CorporateActions.jsx";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { useApi } from "../useApi.js";
import { translate, useT, useLang } from "../i18n.js";
import {
  valuation,
  snapshots,
  getPerformance,
  adminAssetEvidence,
  benchmarks,
} from "../api.js";
import {
  serverText,
  allocationByClass,
  ago,
  area,
  holdingLabel,
  humanize,
  indexPoint,
  money,
  num,
  pct,
  pctShare,
  isLastClose,
  priceStatus,
  perSqm,
  quantity,
  signedToman,
  toman,
  unitPrice,
  isolate,
} from "../format.js";
import {
  AreaTrend,
  Donut,
  MultiLineTrend,
} from "../components/charts.jsx";
import {
  Card,
  Badge,
  Button,
  Delta,
  Tabs,
  Table,
  Empty,
  Async,
  Disclosure,
  toneClass,
  toneFor,
} from "../components/ui.jsx";

const RANGES = [
  { value: "30", label: "30D" },
  { value: "90", label: "90D" },
  { value: "365", label: "1Y" },
  { value: "all", label: "All" },
];

const INFLATION_VIEWS = [
  { value: "nominal", label: "Nominal" },
  { value: "real", label: "vs inflation" },
  { value: "benchmarks", label: "vs markets" },
];

// ponytail: the CPI table is `base 1398=100` (config/settings.py), and a rebase
// is a once-a-decade SCI event, so the base year is a literal here rather than a
// new field on every valuation payload. If it ever moves, serve it from the API.
// Without it "Real Toman" was a number with no unit: 33.6bn nominal showed as
// 1.55bn with nothing on screen saying which year's money that is.
const REAL_BASIS_BASE_YEAR = "1398";
const REAL_BASIS_NOTE = `In constant ${REAL_BASIS_BASE_YEAR} Tomans`;

const ITEM_BADGE = { live: "good", manual: "warn", stale: "warn", quota: "serious", fallback: "serious", unavailable: "critical" };

// Groups valuation items by asset class for the donut. Palette has 8 fixed
// slots (charts.jsx SERIES), so anything past the top 7 folds into "Other"
// rather than cycling colors and losing legend meaning.
/**
 * One chip per market the portfolio holds: open or closed, and how old its
 * newest price is. The total blends a stock market that closed at 12:30 with
 * dollars and coins that are still moving, and a flat stock line beside a
 * live total otherwise reads as a frozen feed. The row keeps its height while
 * loading so nothing below it moves.
 */
function MarketClocks({ data }) {
  const t = useT();
  const markets = data?.markets || [];
  // A dot and a word per market, on the hero's context line. The price age
  // sits in the tooltip: it is the second question, not the first.
  return (
    <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1" data-testid="dashboard-market-clocks">
      {markets.map((m) => {
        const age = m.last_priced_at ? (Date.now() - Date.parse(m.last_priced_at)) / 1000 : null;
        return (
          <span
            key={m.market}
            data-testid={`dashboard-market-${m.market}`}
            title={age != null ? t("Last price {age}", { age: ago(age) }) : undefined}
            className="inline-flex items-center gap-1.5"
          >
            <span
              aria-hidden="true"
              className={`size-1.5 rounded-full ${m.open ? "bg-[var(--c-good)]" : "bg-[var(--c-muted)]"}`}
            />
            <span>{t(m.open ? "{market} open" : "{market} closed", { market: t(m.label) })}</span>
          </span>
        );
      })}
      <Link
        to="/?view=health"
        data-testid="dashboard-data-health-link"
        className="inline-flex items-center gap-1 rounded-full border border-border bg-panel-2 px-2.5 py-0.5 text-xs font-medium text-text hover:border-accent"
      >
        {t("Data health")}
      </Link>
    </span>
  );
}

function HeroRow({ state, basis: selected }) {
  const t = useT();
  return (
    // Reserved so nothing below it moves when the totals replace the spinner:
    // the hero is the first thing on the page.
    <div className="min-h-[112px]">
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
        // Asked for dollars, answered in Toman: the server had no rate to
        // convert by. Without saying so the toggle looks like it did nothing.
        const noRate = selected !== "nominal_toman" && basis === "nominal_toman";
        const cash = Number(data.cash_tomans);
        return (
          // One number, then one quiet line of context. The "Valued in" tile
          // that used to sit beside it repeated the header's basis switch.
          <div data-testid="dashboard-hero">
            <div data-testid="dashboard-total">
              <div className="text-xs font-medium text-muted">{t("Net worth")}</div>
              <div className="tabular mt-1 text-4xl font-semibold tracking-tight sm:text-5xl">
                {money(Number(data.total), basis)}
              </div>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
              {showUsd && (
                <span className="tabular" data-testid="dashboard-usd">≈ ${num(Number(data.total_usd))}</span>
              )}
              {basis === "real_toman" ? (
                <span>{REAL_BASIS_NOTE}</span>
              ) : cash > 0 ? (
                // Cash is part of the total but has no holding row.
                <span className="tabular">{money(cash, basis)} {t("cash")}</span>
              ) : null}
              {noRate && (
                <span className="text-[var(--c-warn-text)]" data-testid="dashboard-basis-fallback">
                  {t("No USD rate right now — showing Toman")}
                </span>
              )}
              <MarketClocks data={data} />
            </div>
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
  const t = useT();
  if (realGrowth == null) return null;
  const rate = cpi?.applied_annual_rate;
  // Both lines stay in Tomans even when the rest of the page is priced in
  // dollars, because the CPI series measures the Toman. Left unsaid, the chart
  // looks like it ignored the basis selector.
  const foreignBasis = basis !== "nominal_toman" && basis !== "real_toman";
  return (
    <div className="mt-2 space-y-1 text-xs text-muted" data-testid="dashboard-trend-real-note">
      <p>
        {nominalGrowth != null && (
          <>{t("Before inflation: {v} over this window.", { v: `${nominalGrowth >= 0 ? "+" : "−"}${pct(Math.abs(nominalGrowth))}` })}{" "}</>
        )}
        {rate != null && (
          <>{t("Prices rose {v} a year over the same days.", { v: pct(rate) })}{" "}</>
        )}
        {t(realGrowth >= 0 ? "In constant Tomans your net worth is up {v}." : "In constant Tomans your net worth is down {v}.", { v: pct(Math.abs(realGrowth)) })}
        {foreignBasis && (
          <> {t("Both lines are shown in Tomans — an inflation comparison only means something in the currency being inflated.")}</>
        )}
      </p>
      {cpi?.source && (
        <p data-testid="dashboard-trend-cpi-source">
          {t("Inflation index: {source}", { source: serverText(cpi.source) })}
        </p>
      )}
    </div>
  );
}

// What each view of the net-worth chart is, for the card's (i). Said once,
// on request, instead of under the chart on every visit.
const TREND_INFO = {
  nominal: "Your total value each day. Days with no recorded snapshot are rebuilt from that day's prices.",
  real: "The same net worth in constant Tomans. The gap between the two lines is inflation, not performance.",
  benchmarks: "Every line starts at 100, so the gap is relative growth over the window — not the amount of money in each.",
};

const RANGE_WORD = { 30: "30 days", 90: "90 days", 365: "1 year", all: "all time" };

/**
 * First-to-last change over the window, less the money moved in or out:
 * a deposit is not a gain. Before `flows` was subtracted, adding 1B Toman of
 * cash printed "+70%" in green.
 */
function RangeChange({ points, basis, range, flows }) {
  const t = useT();
  const valued = points.filter((p) => p.y != null);
  if (valued.length < 2) return null;
  const first = valued[0].y;
  const last = valued[valued.length - 1].y;
  const moved = flows == null ? 0 : Number(flows) || 0;
  const diff = last - first - moved;
  // Against what was there to start with plus what was added, so a
  // portfolio that began near zero does not report a thousand-percent gain.
  const base = first + Math.max(moved, 0);
  const rel = base > 0 ? diff / base : null;
  const tone = toneClass(toneFor(diff));
  return (
    <div className="mb-3 flex flex-wrap items-baseline gap-x-2 text-sm" data-testid="dashboard-trend-change">
      {/* Left-to-right as one unit: in Persian the sign and the percent
          otherwise reorder into "(0%+) 0 T+". */}
      <bdi dir="ltr" className={`tabular font-semibold ${tone}`}>
        {diff >= 0 ? "+" : "−"}{money(Math.abs(diff), basis)}
        {rel != null && ` (${diff >= 0 ? "+" : "−"}${pct(Math.abs(rel))})`}
      </bdi>
      <span className="text-muted">{t(RANGE_WORD[range] || "")}</span>
      {moved !== 0 && (
        <span className="text-xs text-muted" data-testid="dashboard-trend-flows">
          {t(moved > 0 ? "not counting {amount} you added" : "not counting {amount} you took out", {
            amount: money(Math.abs(moved), basis),
          })}
        </span>
      )}
    </div>
  );
}

function TrendCard({ activeId, basis }) {
  const t = useT();
  const lang = useLang();
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
  // A nominal series carries its constant-Toman twin (`data.real`), so
  // switching to "vs inflation" draws immediately instead of paying for a
  // second full snapshot request. That twin needs a CPI figure for every
  // Jalali year the window spans and, like the separate request it replaces,
  // carries an error rather than reusing last year's index.
  const includeReal = trendBasis === "nominal_toman";
  const state = useApi(
    () => snapshots(days, activeId, trendBasis, { includeReal }),
    [days, activeId, trendBasis, includeReal]
  );
  const embeddedReal = state.data?.real;
  // Only for a server that predates `include_real`: a COMPLETED nominal answer
  // without the twin. Right after a basis switch `state` still holds the
  // previous basis's response, not yet marked loading; keyed on its own
  // `basis`, that stale answer cannot start this request -- which nothing
  // would cancel, so the server would compute the second series anyway.
  const realFetch = useApi(
    () => snapshots(days, activeId, "real_toman"),
    [days, activeId],
    {
      enabled: mode === "real" && !state.loading
        && state.data?.basis === "nominal_toman" && !embeddedReal,
    }
  );
  const realState = embeddedReal
    ? (embeddedReal.series
      ? { data: embeddedReal, error: null, loading: false }
      : { data: null, error: new Error(embeddedReal.detail || "Inflation data unavailable."), loading: false })
    : realFetch;
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
      info={<p>{translate(TREND_INFO[mode], lang)}</p>}
      className="h-full"
      actions={(
        <div className="flex flex-wrap items-center gap-2">
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
                {shortfall && (
                  <p className="mt-1 text-xs text-muted" data-testid="dashboard-trend-bench-shortfall">
                    {t("You asked for {a} days and this covers {b}, which is all the history these series share.", { a: requestedDays, b: actualDays })}
                  </p>
                )}
                {(bench.unavailable || []).map((u) => (
                  <p key={u.key} className="mt-1 text-xs text-muted">
                    {t("{name} not shown: {reason}.", { name: t(u.label), reason: serverText(u.reason) })}
                  </p>
                ))}
              </>
            );
          }

          const points = (data.series || []).map((s) => ({ x: s.date, y: s.total == null ? null : Number(s.total) }));
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

          // One point is not a line. A portfolio whose history starts today
          // drew a lone dot on an empty grid.
          if (points.filter((p) => p.y != null).length < 2) {
            return (
              <Empty testId="dashboard-trend-starting" minHeight={260}>
                History starts today. The line fills in day by day.
              </Empty>
            );
          }

          return (
            <>
              {/* The basis the POINTS are in, not the one the picker shows: the
                  previous series stays on screen while the next one loads, and
                  the server answers `nominal_toman` when it had no rate to
                  convert by. Reading it off the data keeps the axis, the tooltip
                  and the numbers describing the same currency. */}
              <RangeChange points={points} basis={data.basis || basis} range={effectiveRange} flows={data.net_flows} />
              <AreaTrend
                data={points}
                longTicks={longTicks}
                basis={data.basis || basis}
              />
              {mode === "real" && realState.error && (
                <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-real-error">
                  {t("No inflation-adjusted series for this window:")} {serverText(realState.error.message)}
                </p>
              )}
              {hasEstimated && (
                <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-note">
                  {estimatedCount === pointCount
                    ? translate("Rebuilt from prices", lang)
                    : translate("{n} of {total} days rebuilt from prices", lang, { n: estimatedCount, total: pointCount })}
                </p>
              )}
              {hasApproximated && (
                <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-hidden-note">
                  {translate("Switched-off assets estimated on some days", lang)}
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
          // Cash is part of the hero total, so it is part of the split (see
          // `allocationByClass`).
          const groups = allocationByClass(data.items, data.cash_tomans);
          if (!groups.length) return <Empty>No priced holdings yet.</Empty>;
          // `Donut` defaults its tooltip to Toman, so this was the last panel on
          // the page still suffixing a converted dollar amount " T" after the
          // hero, the rows and the trend chart were relabelled.
          return (
            <Donut
              data={groups}
              testId="dashboard-donut"
              // Before debts, so it can differ from the net worth above it.
              totalLabel="Total assets"
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
  const t = useT();
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
    { key: "unrealized", header: "Unrealized P&L", align: "right", render: (r) => r.unrealized_pnl_tomans == null ? "—" : (
      // On a phone this number stands alone in the card's detail line, so it names itself.
      <span><span className="me-1 sm:hidden">{t("Gain or loss")}</span><Delta value={r.unrealized_pnl_tomans} format={signedToman} /></span>
    ) },
  ];
  if (showAccount) {
    columns.splice(1, 0, { key: "portfolio", header: "Portfolio", render: (r) => r.account_name || "—" });
  }
  // Two lines per asset on a phone: name and value, then quantity and the
  // P&L that is still open. Seven labelled lines per row, mostly "—", made
  // this card a third of the page.
  const roles = { asset: "title", value: "value", portfolio: "meta", qty: "meta", unrealized: "meta" };
  columns.forEach((c) => { if (roles[c.key]) c.mobile = roles[c.key]; });
  // Once one holding has a cost the "Record purchases" prompt is gone, so the
  // rest need their own way back to the ledger.
  const missing = rows.filter((r) => r.total_cost_basis_tomans == null && r.realized_pnl_tomans == null).length;
  return (
    <>
      <Table
        testId="dashboard-performance-table"
        caption="Position performance"
        mobileCards
        rowKey={(r) => r.key}
        rows={rows}
        columns={columns}
      />
      {missing > 0 && (
        <p className="mt-3 text-xs text-muted" data-testid="dashboard-performance-missing-cost">
          {t(missing === 1 ? "1 holding has no cost yet." : "{n} holdings have no cost yet.", { n: missing })}{" "}
          <Link to="/activity" className="text-[var(--c-accent-text)] underline underline-offset-2">{t("Add what you paid")}</Link>
        </p>
      )}
    </>
  );
}

/**
 * A positions table is only worth showing when it says something the holdings
 * table above does not: what was paid. With no purchases recorded every cost
 * and P&L cell is "—" and the rest repeats the holdings, so say what unlocks it.
 */
function hasCostData(rows) {
  return rows.some((r) => r.total_cost_basis_tomans != null || r.realized_pnl_tomans != null);
}

function NoCostYet() {
  const t = useT();
  return (
    <Empty
      testId="dashboard-performance-no-cost"
      action={<Link to="/activity" className="inline-flex min-h-10 items-center rounded-md border border-border bg-panel-2 px-3 text-sm font-medium text-text hover:bg-border sm:min-h-8">{t("Record purchases")}</Link>}
    >
      Record what you paid to see profit and loss.
    </Empty>
  );
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
      if (Boolean(a.is_hidden) !== Boolean(b.is_hidden)) return a.is_hidden ? 1 : -1;
      const valueDelta = Number(b.value || 0) - Number(a.value || 0);
      return valueDelta || holdingLabel(a).localeCompare(holdingLabel(b));
    })
  );
}

// The status words in the holdings table, behind the card's (i).
const PRICING_TERMS = [
  ["Live", "priced within the last 5 minutes, or the last price from the latest session while that market is shut."],
  ["Manual", "house or real-estate values updated within the last 90 days."],
  ["Stale", "the quote is from a previous session; a fresher one should have arrived."],
  ["Daily limit", "our price source's daily limit is reached; showing the last known price, which is not current."],
  ["Mixed", "some prices are old, limited, or from our archive."],
  ["Real Toman", "adjusted for inflation using the Statistical Center of Iran's published CPI. The year in progress has no release yet, so that stretch is a labelled projection; the \"vs inflation\" view prints the rate it used."],
];

function PricingGlossary() {
  const t = useT();
  return (
    <div data-testid="dashboard-pricing-glossary">
      <ul className="space-y-1">
        {PRICING_TERMS.map(([term, meaning]) => (
          <li key={term}><strong className="text-text">{t(term)}</strong> — {t(meaning)}</li>
        ))}
      </ul>
    </div>
  );
}

const HOLDING_VIEWS = [
  { value: "value", label: "Value" },
  { value: "return", label: "Profit & cost" },
];

function HoldingsCard({ activeId, valuationState, performanceState, portfolio, admin }) {
  const t = useT();
  const [viewMode, setViewMode] = useState("value");
  const [whyKey, setWhyKey] = useState(null);
  const [adding, setAdding] = useState(false);

  const reloadAll = () => {
    valuationState.reload();
    performanceState?.reload?.();
    portfolio.reload();
  };

  // Flat holding rows with their owning account attached — what the add dialog
  // needs to revalue a property it already holds.
  const allHoldings = portfolio.accounts.flatMap((a) =>
    (a.holdings || []).map((h) => ({ ...h, account_id: a.id }))
  );

  const cardActions = (
    <div className="flex flex-wrap items-center gap-2">
      <Tabs
        options={HOLDING_VIEWS}
        value={viewMode}
        onChange={setViewMode}
        label="Holdings view"
        testId="dashboard-holdings-view"
      />
      <Link
        to="/activity"
        data-testid="dashboard-holdings-manage-link"
        className="inline-flex min-h-9 items-center rounded-md border border-border bg-panel-2 px-3 text-xs font-medium text-text hover:border-accent"
      >
        {t("Edit in Activity")}
      </Link>
      <Button
        variant="primary"
        onClick={() => setAdding(true)}
        data-testid="dashboard-add-button"
      >
        Add an asset
      </Button>
    </div>
  );

  return (
    <Card title="Holdings" testId="dashboard-holdings" actions={cardActions} info={<PricingGlossary />}>
      <Async {...valuationState} testId="dashboard-holdings-body">
        {(data) => {
          const rowBasis = data.basis || portfolio.basis;
          const visible = data.items || [];
          const hidden = data.hidden_items || [];
          const items = sortHoldingsByPortfolioValue([...visible, ...hidden]);
          const weightBase =
            visible.reduce((sum, i) => sum + Number(i.value || 0), 0) +
              Math.max(Number(data.cash_tomans) || 0, 0) || 1;
          const priceable = visible.filter((i) => i.quality_status !== "manual");
          const staleCount = priceable.filter((i) => i.quality_status && i.quality_status !== "live").length;
          const showStaleBanner = priceable.length > 0 && staleCount / priceable.length >= 0.5;

          const columns = [
            {
              key: "asset",
              header: "Asset",
              render: (r) => (
                <bdi title={[r.name_fa || r.asset, r.class ? humanize(r.class) : ""].filter(Boolean).join(" · ")}>
                  {holdingLabel(r)}
                </bdi>
              ),
            },
            {
              key: "qty",
              header: "Size / quantity",
              align: "right",
              render: (r) => (r.is_house ? area(r.area_sqm) : quantity(r.quantity, r.quantity_step)),
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
              render: (r) => (r.is_hidden ? "—" : pctShare(Number(r.value) / weightBase)),
            },
            {
              key: "status",
              header: "Status",
              render: (r) => {
                const ageText = r.priced_at ? ago(r.age_seconds) : (r.archive_record?.date || "");
                return (
                  <div className="inline-flex flex-wrap items-center gap-1 align-middle" title={ageText || undefined}>
                    {r.is_hidden && <Badge variant="warn">Not counted</Badge>}
                    <Badge variant={isLastClose(r, data.markets) ? "neutral" : ITEM_BADGE[r.quality_status] || "neutral"}>{priceStatus(r, data.markets)}</Badge>
                    {r.price_unit_status === "unverified" && <Badge variant="warn">unverified unit</Badge>}
                  </div>
                );
              },
            },
          ];

          if (activeId == null) {
            columns.unshift({
              key: "portfolio",
              header: "Portfolio",
              render: (r) => (
                <span dir="auto" className="inline-block max-w-[11rem] truncate align-bottom" title={r.account_name || ""}>
                  {r.account_name || "—"}
                </span>
              ),
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

          const MOBILE_ROLES = {
            asset: "title", value: "value",
            portfolio: "meta", qty: "meta", status: "meta",
          };
          columns.forEach((c) => { if (MOBILE_ROLES[c.key]) c.mobile = MOBILE_ROLES[c.key]; });

          return (
            <>
              {viewMode === "return" ? (
                <Async {...performanceState} testId="dashboard-holdings-return-body">
                  {(perfData) => {
                    const positions = perfData.aggregate
                      ? perfData.accounts.flatMap((row) => positionRows(row.assets, row.name))
                      : positionRows(perfData.assets);
                    if (!positions.length || !hasCostData(positions)) {
                      return <NoCostYet />;
                    }
                    return <PositionsTable rows={positions} showAccount={Boolean(perfData.aggregate)} />;
                  }}
                </Async>
              ) : (
                <>
                  {showStaleBanner && (
                    <div
                      role="status"
                      data-testid="dashboard-stale-banner"
                      className="mb-3 rounded-lg border border-[var(--c-warn)]/40 bg-[var(--c-warn)]/10 px-4 py-3 text-sm"
                    >
                      {t("{n} of {total} prices aren't live — showing the last known price.", { n: staleCount, total: priceable.length })}
                    </div>
                  )}
                  {hidden.length > 0 && (
                    <p className="mb-3 text-sm text-muted" data-testid="dashboard-holdings-hidden-note">
                      {t(hidden.length === 1 ? "1 asset switched off and left out of every figure." : "{n} assets switched off and left out of every figure.", { n: hidden.length })}
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
                    rowClass={(r) => (r.is_hidden ? "text-muted" : "")}
                  />
                </>
              )}
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
  const data = valuationState.data;
  const excluded = data?.excluded;
  if (!excluded?.length) return null;
  // Named as the holdings table names them. This printed the catalog key
  // (usd_cash, swiss_gold_bar_2_5g) -- an identifier, not a name.
  const rows = [...(data.items || []), ...(data.hidden_items || [])];
  const nameOf = (key) => {
    const row = rows.find((r) => r.key === key);
    return row ? holdingLabel(row) : humanize(key);
  };
  return (
    <Disclosure summary="Assets excluded from this valuation" testId="dashboard-excluded">
      <ul className="space-y-1">
        {excluded.map((e) => (
          <li key={`${e.account_id}:${e.asset_key}`}>
            <bdi>{nameOf(e.asset_key)}</bdi> — {humanize(e.reason)}
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


export default function Dashboard({ user }) {
  const t = useT();
  const portfolio = usePortfolio();
  const { activeId, basis } = portfolio;
  const valuationState = useApi(() => valuation(activeId, basis), [activeId, basis], { pollMs: 60000 });
  const perfTargets = performanceTargets(activeId, portfolio.accounts);
  const accountKey = portfolio.accounts
    .map((a) => `${a.id}:${a.ledger_complete}:${JSON.stringify(a.holdings ?? [])}`)
    .join("|");
  const performanceState = useApi(
    () => fetchPerformance(perfTargets, portfolio.accounts, basis),
    [activeId, basis, accountKey, perfTargets.ids.join(",")],
    { enabled: perfTargets.ids.length > 0 }
  );

  // A portfolio with nothing in it -- just created, most often. Every card
  // below would say "nothing yet" in its own words around a "0 T"; one card
  // saying what to do first is the whole page.
  const v = valuationState.data;
  const holdsNothing =
    activeId != null && v && !valuationState.loading &&
    !(v.items || []).length && !(v.hidden_items || []).length &&
    !Number(v.cash_tomans) && !Number(v.total_liabilities);
  // Nothing today is not the same as never anything: a portfolio emptied by
  // a withdrawal still has a history to draw, and that keeps the full page.
  const pastState = useApi(() => snapshots(365, activeId), [activeId], { enabled: Boolean(holdsNothing) });
  const hadSomething = (pastState.data?.series || []).some((p) => Number(p.total));
  const isEmpty = holdsNothing && !pastState.loading && !pastState.error && !hadSomething;
  if (isEmpty) {
    const name = portfolio.accounts.find((a) => a.id === activeId)?.name;
    return (
      <div>
        <h1 className="sr-only">Portfolio</h1>
        <Card testId="dashboard-empty-portfolio">
          <Empty
            minHeight={280}
            action={
              <Link
                to="/activity?add=trade"
                data-testid="dashboard-empty-add"
                className="inline-flex min-h-10 items-center rounded-md bg-[var(--c-accent-fill)] px-4 text-sm font-medium text-white hover:opacity-90"
              >
                {t("Add the first asset")}
              </Link>
            }
          >
            <span className="block text-base font-semibold text-text">{name ? t("{name} is empty", { name: isolate(name) }) : t("This portfolio is empty")}</span>
            <span className="mt-1 block">{t("Record something you own, or money you put in.")}</span>
          </Empty>
        </Card>
      </div>
    );
  }

  return (
    <div>
      <h1 className="sr-only">Portfolio</h1>
      <div className="space-y-6">
        <HeroRow state={valuationState} basis={basis} />
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
        <HoldingsCard activeId={activeId} valuationState={valuationState} performanceState={performanceState} portfolio={portfolio} admin={user?.role === "admin"} />
        <Disclosure summary="Debts & excluded assets" testId="dashboard-more">
          <div className="space-y-4">
            <LiabilitiesCard activeId={activeId} accounts={portfolio.accounts} onChanged={valuationState.reload} />
            <ExcludedDisclosure valuationState={valuationState} />
          </div>
        </Disclosure>
      </div>
    </div>
  );
}

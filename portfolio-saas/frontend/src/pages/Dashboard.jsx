import { useState } from "react";
import { Link } from "react-router-dom";
import AddTransactionDialog from "../components/AddTransactionDialog.jsx";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { useApi } from "../useApi.js";
import {
  valuation,
  snapshots,
  getPerformance,
  updateHolding,
  removeHolding,
  adminAssetEvidence,
  analytics,
  diversifiers,
  benchmarks,
} from "../api.js";
import {
  ago,
  area,
  assetLabel,
  holdingLabel,
  humanize,
  indexPoint,
  money,
  num,
  pct,
  perfLabel,
  perSqm,
  PERF_UNLOCK_HINT,
  signedToman,
  toman,
  unitPrice,
} from "../format.js";
import {
  AreaTrend,
  CorrelationHeatmap,
  DiversifierScatter,
  Donut,
  MoneyVsRisk,
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
  { value: "benchmarks", label: "vs gold & USD" },
];

// The benchmark endpoint only accepts 90/180/365-day windows, so the comparison
// offers exactly those. Mapping "30d" onto a 90-day request made the selected
// button say 30d while the axis showed three months.
const BENCH_RANGES = [
  { value: "90", label: "90d" },
  { value: "365", label: "1y" },
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

const QUALITY_BADGE = { complete: "good", manual: "warn", partial: "warn", unavailable: "critical" };
const QUALITY_LABEL = { complete: "Live", manual: "Manual", partial: "Mixed", unavailable: "Unavailable" };
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

function HeroRow({ state, basis: selected }) {
  return (
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
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-4" data-testid="dashboard-hero">
            <div className="sm:col-span-2">
              <StatTile
                label="Total value"
                value={money(Number(data.total), basis)}
                size="lg"
                sub={basis === "real_toman" ? REAL_BASIS_NOTE : undefined}
                testId="dashboard-total"
              />
            </div>
            <StatTile
              label={showUsd ? "USD equivalent" : "Valued in"}
              value={showUsd ? "$" + num(Number(data.total_usd)) : BASIS_LABEL[basis]}
              testId="dashboard-usd"
            />
            <StatTile
              label="Priced holdings"
              value={`${data.priced_assets}/${data.total_assets}`}
              sub={
                <Badge variant={QUALITY_BADGE[data.quality_status] || "neutral"} testId="dashboard-quality-badge">
                  {QUALITY_LABEL[data.quality_status] || humanize(data.quality_status)}
                </Badge>
              }
              testId="dashboard-coverage"
            />
          </div>
        );
      }}
    </Async>
  );
}

function TrendCard({ activeId, basis }) {
  const [range, setRange] = useState("30");
  const [mode, setMode] = useState("nominal");
  // Ranges differ per mode, so a range the current mode cannot honour falls back
  // rather than silently showing a different window than the one selected.
  // EVERY series on this chart has to follow that fallback, not just the tab
  // highlight and the benchmark fetch: deriving `days` from the raw `range` left
  // the net-worth line on 30 days while the gold/USD lines covered 90, and both
  // were rebased to 100 on the same axis, so the "relative growth" gap compared
  // three months of benchmark against one month of portfolio.
  const rangeOptions = mode === "benchmarks" ? BENCH_RANGES : RANGES;
  const effectiveRange = rangeOptions.some((r) => r.value === range)
    ? range
    : rangeOptions[0].value;
  const days = effectiveRange === "all" ? "all" : Number(effectiveRange);
  const state = useApi(() => snapshots(days, activeId, basis), [days, activeId, basis]);
  // The same net worth measured in constant Tomans. Fetched only when asked,
  // because it needs a CPI figure for every Jalali year the window spans and
  // fails loudly rather than silently reusing last year's index.
  const realState = useApi(
    () => snapshots(days, activeId, "real_toman"),
    [days, activeId],
    { enabled: mode === "real" }
  );
  const benchState = useApi(
    () => benchmarks(activeId, { window: Number(effectiveRange) }),
    [activeId, effectiveRange],
    { enabled: mode === "benchmarks" }
  );

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
            options={rangeOptions}
            value={effectiveRange}
            onChange={setRange}
            label="Range"
            testId="dashboard-trend-tabs"
          />
        </div>
      )}
    >
      <Async {...state} testId="dashboard-trend-body" empty="No history yet.">
        {(data) => {
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

          if (mode === "benchmarks" && benchState.data?.series?.length) {
            const bench = benchState.data;
            const keys = Object.keys(bench.labels);
            const last = bench.series[bench.series.length - 1];
            return (
              <>
                <MultiLineTrend
                  series={keys.map((k) => ({ key: k, name: bench.labels[k] }))}
                  data={bench.series}
                  longTicks={longTicks}
                  formatValue={indexPoint}
                  formatAxis={indexPoint}
                  label="Your portfolio against gold and the dollar, indexed to 100"
                />
                <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-bench-note">
                  Each line starts at 100, so the gap is relative growth over the
                  window — not the amount of money in each.
                </p>
                {(bench.unavailable || []).map((u) => (
                  <p key={u.key} className="mt-1 text-xs text-muted">
                    {u.label} not shown: {u.reason}.
                  </p>
                ))}
              </>
            );
          }

          if (mode === "real" && realState.data?.series?.length) {
            const real = new Map(
              realState.data.series.map((s) => [s.date, Number(s.total)])
            );
            const merged = points.map((p) => ({ x: p.x, nominal: p.y, real: real.get(p.x) ?? null }));
            const first = merged.find((m) => m.real != null);
            const last = [...merged].reverse().find((m) => m.real != null);
            const realGrowth = first && last && first.real ? last.real / first.real - 1 : null;
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
                {realGrowth != null && (
                  <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-real-note">
                    In constant Tomans your net worth is {realGrowth >= 0 ? "up" : "down"}{" "}
                    {pct(Math.abs(realGrowth))} over this window. The gap between the two
                    lines is inflation, not performance.
                  </p>
                )}
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
      <Async {...state} testId="dashboard-allocation-body" empty="No priced holdings yet.">
        {(data) => {
          const groups = groupByClass(data.items || []);
          if (!groups.length) return <Empty>No priced holdings yet.</Empty>;
          return <Donut data={groups} testId="dashboard-donut" />;
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
              return <PerformanceUnavailable detail={data.accounts[0]?.detail} />;
            }
            return (
              <>
                {activeId == null && accounts.length > 1 && (
                  <p className="mb-3 text-xs text-muted">All portfolios with a completed opening baseline.</p>
                )}
                <Table
                  testId="dashboard-performance-table"
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
          if (!data.performance_available) {
            return <PerformanceUnavailable detail={data.detail} />;
          }
          const rows = Object.entries(data.assets || {}).map(([key, v]) => ({ key, ...v }));
          return (
            <>
              <PerformanceMetrics data={data} />
              <div className="mt-4">
                <Table
                  testId="dashboard-performance-table"
                  rowKey={(r) => r.key}
                  rows={rows}
                  columns={[
                    { key: "asset", header: "Asset", render: (r) => r.asset_name },
                    { key: "qty", header: "Quantity", align: "right", render: (r) => num(r.quantity, 4) },
                    { key: "avg", header: "Avg cost", align: "right", render: (r) => toman(r.average_cost_tomans) },
                    { key: "basis", header: "Cost basis", align: "right", render: (r) => toman(r.total_cost_basis_tomans) },
                    { key: "realized", header: "Realized P&L", align: "right", render: (r) => <Delta value={r.realized_pnl_tomans} format={signedToman} /> },
                    { key: "unrealized", header: "Unrealized P&L", align: "right", render: (r) => <Delta value={r.unrealized_pnl_tomans} format={signedToman} /> },
                  ]}
                />
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

function isManualPriceEditable(row) {
  return !row.is_house && (
    row.is_manual || row.quality_status === "manual" || row.source === "manual_valuation"
  );
}

// For a property `qty` is the price per square meter in millions of Toman — the
// column the API stores it in — and `area` is its size. Those are the two numbers
// a property is described by; the raw `quantity` is never shown on its own.
function draftForRow(row, drafts) {
  const key = holdingsRowKey(row);
  return drafts[key] ?? {
    qty: String(row.quantity ?? ""),
    price: row.unit_price != null && row.unit_price !== "" ? String(row.unit_price) : "",
    area: row.area_sqm != null ? String(row.area_sqm) : "",
  };
}

function hasDraftChanges(row, draft) {
  const origQty = String(row.quantity ?? "");
  const origPrice = row.unit_price != null && row.unit_price !== "" ? String(row.unit_price) : "";
  const origArea = row.area_sqm != null ? String(row.area_sqm) : "";
  if (draft.qty.trim() !== origQty) return true;
  if (row.is_house) return draft.area.trim() !== origArea;
  return isManualPriceEditable(row) && draft.price.trim() !== origPrice;
}

/** Manual and real-estate rows are the ones whose name is the user's to choose. */
function isRenamable(row) {
  return !!(row.is_house || row.is_manual);
}

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
        <li><strong className="text-text">Real Toman</strong> — inflation-adjusted using SCI's CPI series through 1404.</li>
      </ul>
    </Disclosure>
  );
}

function HoldingsCard({ activeId, valuationState, portfolio, staff }) {
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
    setManageMode((current) => {
      const next = current === mode ? null : mode;
      if (next === "edit") portfolio.reload();
      return next;
    });
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
    const key = holdingsRowKey(row);
    setSavingKey(key);
    setActionError(null);
    try {
      await updateHolding(accountId, holding.id, {
        quantity: qty,
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
          const items = [...visible, ...hidden];
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
                if (manageMode === "edit" && holding && isRenamable(r)) {
                  return (
                    <input
                      className={`${inlineInputClass} text-left`}
                      defaultValue={r.display_name || ""}
                      placeholder={r.name_fa || r.asset}
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
                return holdingLabel(r);
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
                  return (
                    <input
                      type="number"
                      step="any"
                      className={inlineInputClass}
                      value={draft.qty}
                      aria-label={`Quantity for ${holdingLabel(r)}`}
                      data-testid="dashboard-holdings-edit-qty"
                      disabled={savingKey === rk}
                      onChange={(e) => setDraftField(r, "qty", e.target.value)}
                    />
                  );
                }
                return num(r.quantity, 4);
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
                return unitPrice(r.unit_price, r.unit_price_currency, rowBasis);
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

          if (staff) {
            columns.push({
              key: "why",
              header: "",
              render: (r) => (
                <Button variant="ghost" onClick={() => setWhyKey(r.key)} data-testid="dashboard-why">Why</Button>
              ),
            });
          }

          if (manageMode === "edit") {
            columns.push({
              key: "actions",
              header: "",
              align: "right",
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
            columns.push({
              key: "actions",
              header: "",
              align: "right",
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
                <p className="mb-3 text-xs text-muted">
                  Change any quantity and click Save. Manual assets also take a unit price; a
                  property takes its size and what a square meter is worth, in millions of Toman.
                  Names of your own assets save as soon as you click away.
                </p>
              )}
              <Table
                testId="dashboard-holdings-table"
                rowKey={holdingsRowKey}
                rows={items}
                columns={columns}
                empty="No holdings priced yet."
                rowClass={(r) => (r.is_hidden ? "opacity-50" : "")}
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
              {staff && whyKey && <WhyDrawer assetKey={whyKey} onClose={() => setWhyKey(null)} />}
            </>
          );
        }}
      </Async>
    </Card>
  );
}



const RISK_WINDOWS = [
  { value: "90", label: "90d" },
  { value: "180", label: "180d" },
  { value: "365", label: "365d" },
];

function riskShareRows(weights = {}, risks = {}, labelFor = (key) => key) {
  const keys = [...new Set([...Object.keys(weights), ...Object.keys(risks)])];
  return keys
    .map((key) => {
      const weight_share = Number(weights[key] || 0);
      const risk_share = Number(risks[key] || 0);
      return {
        key: labelFor(key),
        weight_share,
        risk_share,
        gap: risk_share - weight_share,
      };
    })
    .filter((row) => row.weight_share > 0 || row.risk_share > 0)
    .sort((a, b) => b.gap - a.gap);
}

/** One stacked panel: a heading, a sentence saying what to look for, the chart. */
function RiskPanel({ title, caption, children }) {
  return (
    <section className="border-t border-border pt-5 first:border-0 first:pt-0">
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="mt-1 mb-3 max-w-prose text-xs text-muted">{caption}</p>
      {children}
    </section>
  );
}

function RiskClassView({ data, valueByClass }) {
  const div = data.diversification || {};
  const rows = riskShareRows(div.weight_by_class, div.risk_by_class);
  if (!rows.length) return <Empty testId="dashboard-risk-class-empty">No class risk data.</Empty>;
  return (
    <div data-testid="dashboard-risk-class-view">
      <MoneyVsRisk
        rows={rows}
        label="Share of money versus share of risk by asset class"
        coverage={div.mean_weight_covered}
        valueFor={(key) => valueByClass[key]}
        testId="risk-money-vs-risk-class"
      />
    </div>
  );
}

function RiskAssetView({ data, labelFor, valueByLabel }) {
  const div = data.diversification || {};
  const rows = (div.concentration_gap || []).map((row) => ({
    ...row,
    key: labelFor(row.key),
  }));
  if (!rows.length) return <Empty testId="dashboard-risk-asset-empty">No asset risk data.</Empty>;
  return (
    <div data-testid="dashboard-risk-asset-view">
      <MoneyVsRisk
        rows={rows}
        label="Share of money versus share of risk by asset"
        coverage={div.mean_weight_covered}
        valueFor={(key) => valueByLabel[key]}
        testId="risk-money-vs-risk"
      />
    </div>
  );
}

function RiskCorrelationView({ data, labelFor }) {
  const correlation = data.correlation || {};
  if ((correlation.assets || []).length < 2) {
    return <Empty testId="dashboard-risk-correlation-empty">Not enough overlapping assets.</Empty>;
  }
  return (
    <div data-testid="dashboard-risk-correlation-view">
      <CorrelationHeatmap
        assets={correlation.assets.map(labelFor)}
        matrix={correlation.matrix}
        testId="risk-correlation"
      />
    </div>
  );
}

function RiskAddView({ activeId, basis, window, labelFor }) {
  const state = useApi(
    () => diversifiers(activeId, { basis, window: Number(window) }),
    [activeId, basis, window]
  );
  return (
    <Async {...state} testId="dashboard-risk-add-body">
      {(data) => {
        if (!data.candidates?.length) {
          // Addressable, like every other panel's empty state: `Async` renders
          // its own testId only when it does NOT reach this branch.
          return (
            <Empty testId="dashboard-risk-add-empty">
              No candidate has enough overlapping history to score yet.
            </Empty>
          );
        }
        return (
          <div data-testid="dashboard-risk-add-view">
            <DiversifierScatter
              candidates={data.candidates.map((row) => ({ ...row, key: labelFor(row.key) }))}
              held={(data.held || []).map((row) => ({ ...row, key: labelFor(row.key) }))}
              testId="risk-diversifier-scatter"
            />
          </div>
        );
      }}
    </Async>
  );
}

/**
 * All four risk views, stacked.
 *
 * They used to be behind tabs, which meant three of the four were never seen and
 * the section answered whichever question the user happened to click. Stacking
 * makes the page longer and shows the whole picture, which is the point of it.
 * The time-window control stays because it changes what every panel measures.
 */
function RiskCard({ activeId, basis, valuationState }) {
  const [window, setWindow] = useState("180");
  const state = useApi(
    () => analytics(activeId, { basis, window: Number(window) }),
    [activeId, basis, window]
  );
  const items = valuationState?.data?.items || [];
  const labelFor = (key) => {
    const item = items.find((i) => i.key === key);
    return item ? holdingLabel(item) : assetLabel({ key });
  };
  // The charts identify rows by their display label, so the amount has to be
  // reachable under the same key the tooltip is handed.
  const valueByLabel = {};
  const valueByClass = {};
  for (const item of items) {
    const value = Number(item.value || 0);
    valueByLabel[holdingLabel(item)] = value;
    const cls = humanize(item.class || "other");
    valueByClass[cls] = (valueByClass[cls] || 0) + value;
  }

  return (
    <Card
      title="Risk"
      subtitle="Where your money sits, where your risk actually comes from, and what would spread it out."
      testId="dashboard-risk"
      actions={(
        <Tabs
          options={RISK_WINDOWS}
          value={window}
          onChange={setWindow}
          label="Window"
          testId="dashboard-risk-window"
        />
      )}
    >
      <div className="space-y-5">
        <Async {...state} testId="dashboard-risk-body">
          {(data) => (
            <>
              <RiskPanel
                title="Risk by class"
                caption="Two dots per row: the share of your money in that class, and the share of your portfolio's swings it accounts for. A risk dot far right of the money dot means that class moves the portfolio more than its size suggests."
              >
                <RiskClassView data={data} valueByClass={valueByClass} />
              </RiskPanel>
              <RiskPanel
                title="Risk by asset"
                caption="The same comparison, one row per holding. The widest gaps are the positions worth trimming first."
              >
                <RiskAssetView data={data} labelFor={labelFor} valueByLabel={valueByLabel} />
              </RiskPanel>
              <RiskPanel
                title="Correlations"
                caption="How closely each pair moves together. Warm cells move in lockstep and give you less protection than owning two things suggests; cool cells pull against each other."
              >
                <RiskCorrelationView data={data} labelFor={labelFor} />
              </RiskPanel>
            </>
          )}
        </Async>
        {/* Its own request and its own Async: a slow or empty diversifier
            response must not hold up the three panels above it. */}
        <RiskPanel
          title="Where diversification would come from"
          caption="Each dot is an asset you could add. Further right means it would calm the portfolio more; higher means it also returned more over the window."
        >
          <RiskAddView
            activeId={activeId}
            basis={basis}
            window={window}
            labelFor={labelFor}
          />
        </RiskPanel>
      </div>
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

export default function Dashboard({ user }) {
  const portfolio = usePortfolio();
  const { activeId, basis } = portfolio;
  const valuationState = useApi(() => valuation(activeId, basis), [activeId, basis], { pollMs: 60000 });

  return (
    <div>
      <PageHeader title="Portfolio" subtitle="Your holdings, valued live, with performance and risk alongside." />
      <div className="space-y-6">
        <HeroRow state={valuationState} basis={basis} />
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <TrendCard activeId={activeId} basis={basis} />
          </div>
          <AllocationCard state={valuationState} />
        </div>
        <PerformanceCard activeId={activeId} basis={basis} accounts={portfolio.accounts} />
        <HoldingsCard activeId={activeId} valuationState={valuationState} portfolio={portfolio} staff={!!user?.is_staff} />
        <ExcludedDisclosure valuationState={valuationState} />
        <RiskCard activeId={activeId} basis={basis} valuationState={valuationState} />
      </div>
    </div>
  );
}

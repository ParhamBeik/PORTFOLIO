import { useState } from "react";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { useApi } from "../useApi.js";
import {
  valuation,
  snapshots,
  getPerformance,
  addHolding,
  updateHolding,
  removeHolding,
  listAssets,
  adminAssetEvidence,
  analytics,
} from "../api.js";
import { num, toman, pct, signedToman, humanize, assetLabel, perfLabel, PERF_UNLOCK_HINT } from "../format.js";
import { AreaTrend, CorrelationHeatmap, Donut, MoneyVsRisk } from "../components/charts.jsx";
import {
  Card,
  StatTile,
  Badge,
  Button,
  Delta,
  Select,
  Input,
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

const QUALITY_BADGE = { complete: "good", manual: "warn", partial: "warn", unavailable: "critical" };
const QUALITY_LABEL = { complete: "Live", manual: "Manual", partial: "Mixed", unavailable: "Unavailable" };
const ITEM_BADGE = { live: "good", manual: "warn", stale: "warn", fallback: "serious", unavailable: "critical" };

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

function HeroRow({ state }) {
  return (
    <Async {...state} testId="dashboard-hero">
      {(data) => {
        const hasUsd = data.total_usd !== undefined && data.total_usd !== null;
        return (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3" data-testid="dashboard-hero">
            <StatTile label="Total value" value={toman(Number(data.total))} testId="dashboard-total" />
            <StatTile
              label="USD equivalent"
              value={hasUsd ? "$" + num(Number(data.total_usd)) : "—"}
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
  const days = range === "all" ? "all" : Number(range);
  const state = useApi(() => snapshots(days, activeId, basis), [days, activeId, basis]);
  return (
    <Card
      title="Net worth"
      testId="dashboard-trend"
      actions={<Tabs options={RANGES} value={range} onChange={setRange} label="Range" testId="dashboard-trend-tabs" />}
    >
      <Async {...state} testId="dashboard-trend-body" empty="No history yet.">
        {(data) => {
          const points = (data.series || []).map((s) => ({ x: s.date, y: Number(s.total) }));
          const hasEstimated = (data.series || []).some((s) => s.is_estimated);
          return (
            <>
              <AreaTrend data={points} longTicks={range === "365" || range === "all"} />
              {hasEstimated && (
                <p className="mt-2 text-xs text-muted" data-testid="dashboard-trend-note">
                  Some points are estimated where a daily snapshot was missing.
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
              return (
                <Empty testId="dashboard-performance-empty">
                  {data.accounts[0]?.detail || PERF_UNLOCK_HINT}
                </Empty>
              );
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
            return (
              <Empty testId="dashboard-performance-empty">
                {data.detail || PERF_UNLOCK_HINT}
              </Empty>
            );
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

function AddHoldingRow({ activeId, onDone }) {
  const assetsState = useApi(listAssets, [], { enabled: activeId != null });
  const [assetKey, setAssetKey] = useState("");
  const [qty, setQty] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const handleAdd = async () => {
    if (!assetKey || !qty) return;
    setSubmitting(true);
    setError(null);
    try {
      await addHolding(activeId, assetKey, qty);
      setAssetKey("");
      setQty("");
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2" data-testid="dashboard-add-holding">
      <Select label="Asset" value={assetKey} onChange={(e) => setAssetKey(e.target.value)} className="min-w-40" data-testid="dashboard-add-asset-select">
        <option value="">Select asset…</option>
        {(assetsState.data || []).map((a) => (
          <option key={a.key} value={a.key}>
            {assetLabel(a)}
          </option>
        ))}
      </Select>
      <Input
        label="Quantity"
        type="number"
        placeholder="Quantity"
        value={qty}
        onChange={(e) => setQty(e.target.value)}
        className="w-28"
        data-testid="dashboard-add-quantity"
      />
      <Button variant="primary" disabled={submitting || !assetKey || !qty} onClick={handleAdd} data-testid="dashboard-add-button">
        Add
      </Button>
      {error && <ErrorState error={error} testId="dashboard-add-error" />}
    </div>
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

function isInlineEditable(row) {
  return row.is_house || isManualPriceEditable(row);
}

function draftForRow(row, drafts) {
  const key = holdingsRowKey(row);
  return drafts[key] ?? {
    qty: String(row.quantity ?? ""),
    price: row.unit_price != null && row.unit_price !== "" ? String(row.unit_price) : "",
  };
}

function hasDraftChanges(row, draft) {
  const origQty = String(row.quantity ?? "");
  const origPrice = row.unit_price != null && row.unit_price !== "" ? String(row.unit_price) : "";
  if (draft.qty.trim() !== origQty) return true;
  return isManualPriceEditable(row) && draft.price.trim() !== origPrice;
}

const inlineInputClass = "w-full min-w-[5rem] rounded-md border border-border bg-panel px-2 py-1 text-right text-sm tabular";

function HoldingsCard({ activeId, valuationState, portfolio, staff }) {
  const [manageMode, setManageMode] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [savingKey, setSavingKey] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [whyKey, setWhyKey] = useState(null);

  const reloadAll = () => {
    valuationState.reload();
    portfolio.reload();
  };

  const holdingsMap = holdingsByAccountAsset(portfolio.accounts);

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
          const total = Number(data.total) || 1;
          const items = data.items || [];

          const columns = [
            { key: "asset", header: "Asset", render: (r) => r.name_fa || r.asset },
            { key: "class", header: "Class", render: (r) => humanize(r.class) },
            {
              key: "qty",
              header: "Quantity",
              align: "right",
              render: (r) => {
                const holding = resolveHolding(r);
                if (manageMode === "edit" && isInlineEditable(r) && holding) {
                  const draft = draftForRow(r, drafts);
                  const rk = holdingsRowKey(r);
                  return (
                    <input
                      type="number"
                      step="any"
                      className={inlineInputClass}
                      value={draft.qty}
                      aria-label={`Quantity for ${r.asset}`}
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
                if (r.is_house) return "—";
                if (manageMode === "edit" && isManualPriceEditable(r) && holding) {
                  const draft = draftForRow(r, drafts);
                  const rk = holdingsRowKey(r);
                  return (
                    <input
                      type="number"
                      step="any"
                      className={inlineInputClass}
                      value={draft.price}
                      aria-label={`Unit price for ${r.asset}`}
                      data-testid="dashboard-holdings-edit-price"
                      disabled={savingKey === rk}
                      onChange={(e) => setDraftField(r, "price", e.target.value)}
                    />
                  );
                }
                return toman(r.unit_price);
              },
            },
            { key: "value", header: "Value", align: "right", render: (r) => toman(r.value) },
            { key: "weight", header: "Weight", align: "right", render: (r) => pct(Number(r.value) / total) },
            {
              key: "status",
              header: "Status",
              render: (r) => (
                <div className="flex flex-wrap items-center gap-1">
                  <Badge variant={ITEM_BADGE[r.quality_status] || "neutral"}>{humanize(r.quality_status)}</Badge>
                  {r.price_unit_status === "unverified" && <Badge variant="warn">unverified unit</Badge>}
                </div>
              ),
            },
            { key: "source", header: "Source", render: (r) => r.source || "—" },
            { key: "priced_at", header: "As of", render: (r) => r.priced_at ? `${r.age_seconds}s` : (r.archive_record?.date || "—") },
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
                if (!isInlineEditable(r)) return null;
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
              {manageMode === "edit" && (
                <p className="mb-3 text-xs text-muted">
                  Manual holdings: edit quantity and unit price, then click Save on each row. Real estate: edit quantity (price per sqm, millions T), then Save.
                </p>
              )}
              <Table testId="dashboard-holdings-table" rowKey={holdingsRowKey} rows={items} columns={columns} empty="No holdings priced yet." />
              {actionError && (
                <div className="mt-2">
                  <ErrorState error={actionError} testId="dashboard-holdings-error" />
                </div>
              )}
              {activeId != null && <AddHoldingRow activeId={activeId} onDone={reloadAll} />}
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

const RISK_VIEWS = [
  { value: "sources", label: "Where risk comes from" },
  { value: "portfolio", label: "Portfolio" },
  { value: "class", label: "By class" },
  { value: "asset", label: "By asset" },
];

const RISK_STATUS_BADGE = {
  ready: "good",
  partial: "warn",
  excluded: "critical",
  insufficient: "warn",
  not_applicable: "neutral",
};

// `proxied` says the series came from a stand-in asset (a Swiss bar priced off
// gold); everything else says the underlying data is thinner than it looks.
const RISK_WARNING_TONE = { proxied: "neutral" };

function RiskWarnings({ row }) {
  const warnings = row.warnings || [];
  if (!warnings.length) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1" data-testid={`dashboard-risk-warnings-${row.key}`}>
      {warnings.map((w) => (
        <Badge key={w} variant={RISK_WARNING_TONE[w] || "warn"}>
          {w === "proxied" && row.proxied_from ? `Proxied via ${row.proxied_from}` : humanize(w)}
        </Badge>
      ))}
    </div>
  );
}

function calmarValue(metrics) {
  if (!metrics) return "—";
  if (metrics.calmar != null) return num(metrics.calmar);
  const months = Math.floor((metrics.calmar_window_days || 0) / 30);
  return `needs 36 months (have ${months})`;
}

function riskHealthLabel(health) {
  if (health === "healthy") return "Healthy";
  if (health === "degraded") return "Degraded";
  return "Unhealthy";
}

function riskHealthTone(health) {
  if (health === "healthy") return "good";
  if (health === "degraded") return "warn";
  return "critical";
}

function BenchmarkTiles({ metrics }) {
  if (!metrics || metrics.benchmark_status === "unavailable") {
    return (
      <StatTile
        label="Beta / alpha vs. benchmark"
        value="Unavailable"
        sub={humanize(metrics?.benchmark_status_reason) || "No benchmark index history"}
        testId="dashboard-risk-benchmark"
      />
    );
  }
  return (
    <>
      <StatTile label="Beta" value={num(metrics.beta)} testId="dashboard-risk-beta" />
      <StatTile label="Alpha (annualized)" value={pct(metrics.alpha)} valueTone={toneFor(metrics.alpha)} testId="dashboard-risk-alpha" />
      <StatTile label="Tracking error" value={pct(metrics.tracking_error)} testId="dashboard-risk-tracking-error" />
      <StatTile label="Information ratio" value={num(metrics.information_ratio)} valueTone={toneFor(metrics.information_ratio)} testId="dashboard-risk-info-ratio" />
    </>
  );
}

function RiskMetricsGrid({ metrics, prefix = "dashboard-risk" }) {
  if (!metrics) {
    return <p className="text-sm text-muted">Not enough price history for this slice.</p>;
  }
  const hasCvar = metrics.historical_cvar_95_daily != null;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      <StatTile label="Annualized volatility" value={pct(metrics.annualized_volatility)} testId={`${prefix}-volatility`} />
      <StatTile
        label="Max drawdown"
        value={pct(metrics.max_drawdown)}
        valueTone={toneFor(metrics.max_drawdown)}
        sub={`${metrics.days_under_water ?? 0} days under water`}
        testId={`${prefix}-max-drawdown`}
      />
      <StatTile label="Sharpe" value={num(metrics.sharpe)} valueTone={toneFor(metrics.sharpe)} testId={`${prefix}-sharpe`} />
      <StatTile label="Sortino" value={num(metrics.sortino)} valueTone={toneFor(metrics.sortino)} testId={`${prefix}-sortino`} />
      <StatTile label="Calmar" value={calmarValue(metrics)} testId={`${prefix}-calmar`} />
      <StatTile label="Diversification ratio" value={`${num(metrics.diversification_ratio)}×`} testId={`${prefix}-diversification`} />
      <StatTile label="VaR 95% (daily)" value={pct(metrics.historical_var_95_daily)} valueTone={toneFor(metrics.historical_var_95_daily)} testId={`${prefix}-var`} />
      <StatTile
        label="CVaR 95% (daily)"
        value={hasCvar ? pct(metrics.historical_cvar_95_daily) : "Unavailable"}
        valueTone={hasCvar ? toneFor(metrics.historical_cvar_95_daily) : "muted"}
        sub={hasCvar ? undefined : "fewer than 5 tail observations"}
        testId={`${prefix}-cvar`}
      />
      <BenchmarkTiles metrics={metrics} />
    </div>
  );
}

function RiskSummary({ data }) {
  const coverage = data.coverage || {};
  const full = data.portfolio_full || {};
  const health = coverage.health || "degraded";
  return (
    <div className="space-y-3" data-testid="dashboard-risk-summary">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Badge variant={riskHealthTone(health)} testId="dashboard-risk-health">{riskHealthLabel(health)}</Badge>
        <span className="text-xs text-muted">
          {data.history_days || 180}d window · {Math.round(data.periods_per_year || 252)} obs/yr · {humanize(data.basis || "nominal_toman")}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile
          label="Holdings covered"
          value={`${coverage.analyzable_holdings ?? 0}/${coverage.total_holdings ?? 0}`}
          sub={`${pct(coverage.value_analyzable_pct)} of value analyzable`}
          testId="dashboard-risk-coverage"
        />
        <StatTile
          label="Analyzed weight"
          value={pct(coverage.analyzed_weight_pct)}
          sub="Share of the book these metrics describe"
          valueTone={(coverage.analyzed_weight_pct || 0) >= 0.9 ? "good" : "warn"}
          testId="dashboard-risk-analyzed-weight"
        />
        <StatTile label="Excluded" value={num(coverage.excluded_holdings)} sub="Missing history or failed gates" valueTone={(coverage.excluded_holdings || 0) > 0 ? "warn" : "good"} />
        <StatTile label="Full portfolio HHI" value={num(full.concentration_hhi)} sub="Concentration across all priced holdings" testId="dashboard-risk-hhi" />
      </div>
      {(coverage.analyzed_weight_pct ?? 1) < 0.999 && (
        <p className="text-sm text-muted" data-testid="dashboard-risk-scope-note">
          Portfolio metrics cover {pct(coverage.analyzed_weight_pct)} of the book, reweighted to 100%. The rest has no daily
          return history — real estate is valued from marks, not prices.
        </p>
      )}
    </div>
  );
}

/**
 * Where the risk actually comes from — the question the weight split cannot
 * answer. Reads `diversification` and `correlation` off /api/analytics/.
 */
function RiskSourcesView({ data }) {
  const div = data.diversification;
  const gaps = div?.concentration_gap || [];
  const corr = data.correlation || {};
  const covered = Number(div?.mean_weight_covered ?? 1);

  if (!gaps.length) {
    return (
      <Empty>
        {div?.unavailable_reason
          ? `Risk cannot be split yet: ${div.unavailable_reason}.`
          : "No priced holdings to decompose."}
      </Empty>
    );
  }

  const worst = gaps[0];
  return (
    <div className="space-y-6" data-testid="dashboard-risk-sources-view">
      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile
          label="Independent bets"
          value={num(div.effective_bets)}
          testId="risk-effective-bets"
        />
        <StatTile label="Holdings" value={num(div.effective_holdings)} />
        <StatTile label="Diversification ratio" value={`${num(div.diversification_ratio)}×`} />
      </div>
      <p className="text-xs text-muted">
        {num(div.effective_bets)} independent bets across {num(div.effective_holdings)} holdings:
        anything the two numbers disagree about is risk you are paying for twice.
      </p>

      <div>
        <h3 className="mb-1 text-sm font-medium">Share of money versus share of risk</h3>
        <p className="mb-3 text-xs text-muted">
          {worst.gap > 0
            ? `${assetLabel(worst.key)} is ${pct(worst.weight_share)} of the money but ${pct(worst.risk_share)} of the risk.`
            : "No holding carries materially more risk than its size."}
        </p>
        <MoneyVsRisk rows={gaps} testId="risk-money-vs-risk" />
      </div>

      {(corr.assets || []).length > 1 && (
        <div>
          <h3 className="mb-1 text-sm font-medium">How the holdings move together</h3>
          <p className="mb-3 text-xs text-muted">
            Blocks of warm cells are assets that rise and fall as one — they are
            fewer bets than they look.
          </p>
          <CorrelationHeatmap
            assets={corr.assets}
            matrix={corr.matrix}
            testId="risk-correlation"
          />
        </div>
      )}

      {covered < 0.999 && (
        <p className="text-xs text-muted" data-testid="risk-coverage-caveat">
          Measured over {pct(covered)} of the portfolio by weight; the rest lacks
          usable history and is excluded from this decomposition.
        </p>
      )}
    </div>
  );
}

function RiskPortfolioView({ data }) {
  return (
    <div className="space-y-4" data-testid="dashboard-risk-portfolio-view">
      <RiskMetricsGrid metrics={data.metrics} />
      {(data.excluded_assets || []).length > 0 && (
        <div className="rounded-lg border border-border bg-panel-2 p-3 text-sm" data-testid="dashboard-risk-excluded-list">
          <p className="font-medium">Excluded from analyzable portfolio metrics</p>
          <ul className="mt-2 space-y-1 text-xs text-muted">
            {(data.excluded_assets || []).map((e) => (
              <li key={`${e.key}-${e.reason}`}>{e.key} — {humanize(e.reason)}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function RiskClassView({ data }) {
  const rows = data.by_asset_class || [];
  if (!rows.length) return <Empty>No asset classes in this portfolio.</Empty>;
  return (
    <div className="space-y-4" data-testid="dashboard-risk-class-view">
      {rows.map((row) => (
        <div key={row.asset_class} className="rounded-lg border border-border bg-panel-2 p-4" data-testid={`dashboard-risk-class-${row.asset_class}`}>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="font-medium">{row.asset_class}</p>
              <p className="text-xs text-muted">
                {pct(row.weight_in_portfolio)} of portfolio · {row.analyzable_count}/{row.held_count} assets analyzable
              </p>
            </div>
            <Badge variant={RISK_STATUS_BADGE[row.status] || "neutral"}>{humanize(row.status)}</Badge>
          </div>
          {row.status === "not_applicable" ? (
            <p className="text-sm text-muted">Real estate is valued from marks, not daily return history.</p>
          ) : (
            <RiskMetricsGrid metrics={row.metrics} prefix={`dashboard-risk-class-${row.asset_class}`} />
          )}
        </div>
      ))}
    </div>
  );
}

function RiskAssetView({ data }) {
  const rows = data.by_asset || [];
  if (!rows.length) return <Empty>No holdings to analyze.</Empty>;
  return (
    <Table
      testId="dashboard-risk-asset-table"
      rows={rows}
      rowKey={(r) => r.key}
      empty="No holdings."
      columns={[
        {
          key: "asset",
          header: "Asset",
          render: (r) => (
            <div>
              <div>{assetLabel({ name: r.name, key: r.key })}</div>
              <div className="text-xs text-muted">
                {humanize(r.asset_class)}
                {r.observations ? ` · ${r.observations} obs` : ""}
              </div>
            </div>
          ),
        },
        {
          key: "status",
          header: "Status",
          render: (r) => (
            <div>
              <Badge variant={RISK_STATUS_BADGE[r.status] || "neutral"} title={humanize(r.status_reason)}>
                {humanize(r.status)}
              </Badge>
              <RiskWarnings row={r} />
            </div>
          ),
        },
        { key: "weight", header: "Weight", align: "right", render: (r) => pct(r.weight_in_portfolio) },
        {
          key: "vol",
          header: "Vol",
          align: "right",
          render: (r) => (r.metrics ? pct(r.metrics.annualized_volatility) : "—"),
        },
        {
          key: "sharpe",
          header: "Sharpe",
          align: "right",
          render: (r) => (r.metrics ? num(r.metrics.sharpe) : "—"),
        },
        {
          key: "mdd",
          header: "Max DD",
          align: "right",
          render: (r) => (r.metrics ? pct(r.metrics.max_drawdown) : "—"),
        },
        {
          key: "var",
          header: "VaR 95%",
          align: "right",
          render: (r) => (r.metrics ? pct(r.metrics.historical_var_95_daily) : "—"),
        },
        {
          key: "reason",
          header: "Note",
          render: (r) => {
            if (r.status_reason) return humanize(r.status_reason);
            if (r.proxied_from) return `Priced off ${r.proxied_from}`;
            return "—";
          },
        },
      ]}
    />
  );
}

function RiskCard({ activeId, basis }) {
  const [window, setWindow] = useState("180");
  const [view, setView] = useState("sources");
  const state = useApi(
    () => analytics(activeId, { basis, window: Number(window) }),
    [activeId, basis, window]
  );

  return (
    <Card
      title="Risk"
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
      <Async {...state} testId="dashboard-risk-body">
        {(data) => (
          <div className="space-y-4">
            <RiskSummary data={data} />
            <Tabs
              label="Risk breakdown"
              testId="dashboard-risk-view"
              value={view}
              onChange={setView}
              options={RISK_VIEWS}
            />
            {view === "sources" && <RiskSourcesView data={data} />}
            {view === "portfolio" && <RiskPortfolioView data={data} />}
            {view === "class" && <RiskClassView data={data} />}
            {view === "asset" && <RiskAssetView data={data} />}
          </div>
        )}
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

export default function Dashboard({ user }) {
  const portfolio = usePortfolio();
  const { activeId, basis } = portfolio;
  const valuationState = useApi(() => valuation(activeId, basis), [activeId, basis], { pollMs: 60000 });

  return (
    <div>
      <PageHeader title="Portfolio" subtitle="Live = every holding priced ≤5 min ago. Manual = house/bars updated within 90 days. Mixed includes stale or archive fallback. Real Toman uses SCI CPI through 1404." />
      <div className="space-y-5">
        <HeroRow state={valuationState} />
        <TrendCard activeId={activeId} basis={basis} />
        <AllocationCard state={valuationState} />
        <PerformanceCard activeId={activeId} basis={basis} accounts={portfolio.accounts} />
        <HoldingsCard activeId={activeId} valuationState={valuationState} portfolio={portfolio} staff={!!user?.is_staff} />
        <ExcludedDisclosure valuationState={valuationState} />
        <RiskCard activeId={activeId} basis={basis} />
      </div>
    </div>
  );
}

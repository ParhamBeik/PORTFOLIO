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
} from "../api.js";
import { num, toman, pct, signedToman, humanize, assetLabel } from "../format.js";
import { AreaTrend, Donut } from "../components/charts.jsx";
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

function PerformanceCard({ activeId, basis }) {
  const state = useApi(() => getPerformance(activeId, basis), [activeId, basis], { enabled: activeId != null });

  if (activeId == null) {
    return (
      <Card title="Performance" testId="dashboard-performance">
        <Empty testId="dashboard-performance-empty">Select a portfolio to see TWR/XIRR.</Empty>
      </Card>
    );
  }

  return (
    <Card title="Performance" testId="dashboard-performance">
      <Async {...state} testId="dashboard-performance-body">
        {(data) => {
          if (!data.performance_available) {
            return <Empty testId="dashboard-performance-empty">{data.detail || "Record opening balances on the Ledger page to unlock TWR and XIRR."}</Empty>;
          }
          const rows = Object.entries(data.assets || {}).map(([key, v]) => ({ key, ...v }));
          return (
            <>
              <div className="grid grid-cols-2 gap-3 sm:max-w-md">
                <StatTile
                  label="TWR"
                  value={pct(data.twr)}
                  valueTone={toneFor(data.twr)}
                  testId="dashboard-performance-twr"
                />
                <StatTile
                  label="XIRR"
                  value={pct(data.xirr)}
                  valueTone={toneFor(data.xirr)}
                  testId="dashboard-performance-xirr"
                />
              </div>
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

function HoldingsCard({ activeId, valuationState, portfolio, staff }) {
  const [editId, setEditId] = useState(null);
  const [editQty, setEditQty] = useState("");
  const [actionError, setActionError] = useState(null);
  const [whyKey, setWhyKey] = useState(null);

  const reloadAll = () => {
    valuationState.reload();
    portfolio.reload();
  };

  const account = activeId != null ? portfolio.accounts.find((a) => a.id === activeId) : null;
  const holdingsByKey = new Map((account?.holdings || []).map((h) => [h.asset_key, h]));

  const saveEdit = async (holding) => {
    setActionError(null);
    try {
      await updateHolding(activeId, holding.id, editQty);
      setEditId(null);
      reloadAll();
    } catch (e) {
      setActionError(e);
    }
  };

  const handleDelete = async (holding) => {
    if (!window.confirm("Remove this holding?")) return;
    setActionError(null);
    try {
      await removeHolding(activeId, holding.id);
      reloadAll();
    } catch (e) {
      setActionError(e);
    }
  };

  return (
    <Card title="Holdings" testId="dashboard-holdings">
      <Async {...valuationState} testId="dashboard-holdings-body">
        {(data) => {
          const total = Number(data.total) || 1;
          const items = data.items || [];

          const columns = [
            { key: "asset", header: "Asset", render: (r) => r.name_fa || r.asset },
            { key: "class", header: "Class", render: (r) => humanize(r.class) },
            { key: "qty", header: "Quantity", align: "right", render: (r) => num(r.quantity, 4) },
            { key: "price", header: "Unit price", align: "right", render: (r) => toman(r.unit_price) },
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

          if (staff) {
            columns.push({
              key: "why",
              header: "",
              render: (r) => (
                <Button variant="ghost" onClick={() => setWhyKey(r.key)} data-testid="dashboard-why">Why</Button>
              ),
            });
          }

          if (activeId != null) {
            columns.push({
              key: "actions",
              header: "",
              align: "right",
              render: (r) => {
                const holding = holdingsByKey.get(r.key);
                if (!holding) return null;
                if (editId === holding.id) {
                  return (
                    <div className="flex justify-end gap-1">
                      <Input
                        label="New quantity"
                        type="number"
                        value={editQty}
                        onChange={(e) => setEditQty(e.target.value)}
                        className="w-24"
                        data-testid="dashboard-holdings-edit-input"
                      />
                      <Button variant="primary" onClick={() => saveEdit(holding)} data-testid="dashboard-holdings-save">
                        Save
                      </Button>
                      <Button variant="ghost" onClick={() => setEditId(null)}>
                        Cancel
                      </Button>
                    </div>
                  );
                }
                return (
                  <div className="flex justify-end gap-1">
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setEditId(holding.id);
                        setEditQty(String(holding.quantity));
                      }}
                      data-testid="dashboard-holdings-edit-toggle"
                    >
                      Edit
                    </Button>
                    <Button variant="danger" onClick={() => handleDelete(holding)} data-testid="dashboard-holdings-delete">
                      Delete
                    </Button>
                  </div>
                );
              },
            });
          }

          return (
            <>
              <Table testId="dashboard-holdings-table" rowKey={(r) => r.account_id != null ? `${r.account_id}:${r.key}` : r.key} rows={items} columns={columns} empty="No holdings priced yet." />
              {actionError && (
                <div className="mt-2">
                  <ErrorState error={actionError} testId="dashboard-holdings-error" />
                </div>
              )}
              {activeId == null ? (
                <p className="mt-3 text-xs text-muted" data-testid="dashboard-holdings-readonly-hint">
                  Select a portfolio to edit or add holdings.
                </p>
              ) : (
                <AddHoldingRow activeId={activeId} onDone={reloadAll} />
              )}
              {staff && whyKey && <WhyDrawer assetKey={whyKey} onClose={() => setWhyKey(null)} />}
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
        <PerformanceCard activeId={activeId} basis={basis} />
        <HoldingsCard activeId={activeId} valuationState={valuationState} portfolio={portfolio} staff={!!user?.is_staff} />
        <ExcludedDisclosure valuationState={valuationState} />
      </div>
    </div>
  );
}

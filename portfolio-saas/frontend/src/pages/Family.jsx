import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getPerformance, snapshots, valuation } from "../api.js";
import { Donut, MultiLineTrend, StackedShareTrend } from "../components/charts.jsx";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import {
  Async,
  Badge,
  Card,
  Disclosure,
  Empty,
  PageHeader,
  StatTile,
  Table,
  Tabs,
  toneClass,
  toneFor,
} from "../components/ui.jsx";
import { humanize, money, moneyCompact, pct, perfLabel, signedPct } from "../format.js";
import { useApi } from "../useApi.js";

const RANGES = [
  { value: "30", label: "30d" },
  { value: "90", label: "90d" },
  { value: "365", label: "1y" },
  { value: "all", label: "All" },
];

const MAX_CHART_PORTFOLIOS = 8;

function groupByClass(items) {
  const totals = new Map();
  for (const it of items || []) {
    const key = it.class || "other";
    totals.set(key, (totals.get(key) || 0) + Number(it.value || 0));
  }
  return [...totals.entries()]
    .map(([name, value]) => ({ name: humanize(name), value }))
    .sort((a, b) => b.value - a.value);
}

function chartPortfolios(accounts) {
  const sorted = [...accounts].sort((a, b) => Number(b.total) - Number(a.total));
  if (sorted.length <= MAX_CHART_PORTFOLIOS) {
    return sorted.map((a) => ({ key: String(a.id), name: a.name, account: a }));
  }
  const head = sorted.slice(0, MAX_CHART_PORTFOLIOS - 1);
  const tail = sorted.slice(MAX_CHART_PORTFOLIOS - 1);
  return [
    ...head.map((a) => ({ key: String(a.id), name: a.name, account: a })),
    { key: "other", name: "Other", account: null, otherIds: tail.map((a) => a.id) },
  ];
}

async function fetchPortfolioHistory(accounts, days, basis) {
  const dayParam = days === "all" ? "all" : Number(days);
  const replies = await Promise.all(
    accounts.map(async (account) => {
      const reply = await snapshots(dayParam, account.id, basis);
      return { id: account.id, name: account.name, points: reply.series || [], basis: reply.basis };
    })
  );
  // Every reply says which basis it could actually apply; they agree, so the
  // first one that answered speaks for the merged chart. Without it the axis
  // draws converted dollars against a Toman scale.
  const applied = replies.find((r) => r.basis)?.basis || basis;
  return { ...mergeHistory(replies), basis: applied };
}

export function mergeHistory(rows) {
  const dates = new Set();
  for (const row of rows) {
    for (const pt of row.points) dates.add(pt.date);
  }
  const sortedDates = [...dates].sort();
  const byAccount = rows.map((row) => ({
    id: row.id,
    points: new Map(row.points.map((point) => [point.date, point.total])),
  }));

  const values = sortedDates.map((date) => {
    const point = { x: date };
    let total = 0;
    let complete = true;
    for (const row of byAccount) {
      const value = row.points.get(date);
      const amount = value == null ? null : Number(value);
      point[String(row.id)] = amount;
      if (amount == null || !Number.isFinite(amount)) complete = false;
      else total += amount;
    }
    point._total = complete ? total : null;
    return point;
  });

  const shares = values.map((row) => {
    const out = { x: row.x };
    const total = row._total;
    for (const rowMeta of rows) {
      const key = String(rowMeta.id);
      out[key] = total > 0 ? row[key] / total : null;
    }
    return out;
  });

  return { values, shares, rows };
}

function collapseForChart(seriesMeta, values, shares) {
  const other = seriesMeta.find((s) => s.key === "other");
  if (!other?.otherIds?.length) {
    return {
      series: seriesMeta.map((s) => ({ key: s.key, name: s.name })),
      values,
      shares,
    };
  }

  const valueRows = values.map((row) => {
    const next = { x: row.x };
    let otherSum = 0;
    for (const s of seriesMeta) {
      if (s.key === "other") continue;
      next[s.key] = row[s.key];
    }
    for (const id of other.otherIds) {
      if (row[String(id)] == null) otherSum = null;
      else if (otherSum != null) otherSum += row[String(id)];
    }
    next.other = otherSum;
    return next;
  });

  const shareRows = shares.map((row) => {
    const next = { x: row.x };
    let otherSum = 0;
    for (const s of seriesMeta) {
      if (s.key === "other") continue;
      next[s.key] = row[s.key];
    }
    for (const id of other.otherIds) {
      if (row[String(id)] == null) otherSum = null;
      else if (otherSum != null) otherSum += row[String(id)];
    }
    next.other = otherSum;
    return next;
  });

  return {
    series: seriesMeta.map((s) => ({ key: s.key, name: s.name })),
    values: valueRows,
    shares: shareRows,
  };
}

async function fetchAllPerformance(accounts, basis) {
  return Promise.all(
    accounts.map(async (account) => {
      const perf = await getPerformance(account.id, basis);
      return { id: account.id, name: account.name, ...perf };
    })
  );
}

function HistoryCharts({ accounts, basis }) {
  const [range, setRange] = useState("90");
  const days = range === "all" ? "all" : Number(range);
  const accountKey = accounts.map((a) => a.id).join(",");
  const state = useApi(
    () => fetchPortfolioHistory(accounts, days, basis),
    [accountKey, days, basis],
    { enabled: accounts.length > 0 }
  );

  const seriesMeta = useMemo(() => chartPortfolios(accounts), [accounts]);

  return (
    <Card
      title="Share and value over time"
      testId="breakdown-history"
      actions={<Tabs options={RANGES} value={range} onChange={setRange} label="Range" testId="breakdown-history-tabs" />}
    >
      <Async {...state} testId="breakdown-history-body" empty="Not enough history yet.">
        {(history) => {
          const chart = collapseForChart(seriesMeta, history.values, history.shares);
          const longTicks = range === "365" || range === "all";
          if (!chart.values.length) return <Empty>No snapshot history for this range.</Empty>;
          return (
            <div className="space-y-6">
              <div>
                <h3 className="mb-2 text-sm font-medium text-muted">Share of combined total</h3>
                <StackedShareTrend
                  series={chart.series}
                  data={chart.shares}
                  longTicks={longTicks}
                  label="Portfolio share over time"
                />
              </div>
              <div>
                <h3 className="mb-2 text-sm font-medium text-muted">Absolute net worth</h3>
                <MultiLineTrend
                  series={chart.series}
                  data={chart.values}
                  longTicks={longTicks}
                  label="Portfolio values over time"
                  formatValue={(v) => money(v, history.basis)}
                  formatAxis={(v) => moneyCompact(v, history.basis)}
                />
              </div>
            </div>
          );
        }}
      </Async>
    </Card>
  );
}

function PerformanceTable({ accounts, basis }) {
  const accountKey = accounts.map((a) => a.id).join("|");
  const state = useApi(() => fetchAllPerformance(accounts, basis), [accountKey, basis], {
    enabled: accounts.length > 0,
  });

  return (
    <Card title="Performance by portfolio" testId="breakdown-performance">
      <Async {...state} testId="breakdown-performance-body">
        {(rows) => (
          <>
          <Table
            testId="breakdown-performance-table"
            rowKey={(r) => r.id}
            rows={rows}
            columns={[
              { key: "name", header: "Portfolio", render: (r) => r.name },
              {
                key: "twr",
                header: perfLabel.twr,
                align: "right",
                render: (r) =>
                  r.performance_available ? (
                    <span className={toneClass(toneFor(r.twr))}>{signedPct(r.twr)}</span>
                  ) : (
                    "—"
                  ),
              },
              {
                key: "xirr",
                header: perfLabel.xirr,
                align: "right",
                render: (r) =>
                  r.performance_available ? (
                    <span className={toneClass(toneFor(r.xirr))}>{signedPct(r.xirr)}</span>
                  ) : (
                    "—"
                  ),
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
          {/* The same explanation the dashboard card carries. Without it this
              table said "available after 71 more days" while My Optimal printed
              a 1-year return and Comparison a 90-day one for the same holdings,
              and the three read as three answers to one question. */}
          {rows.some((r) => !r.performance_available) && (
            <p className="mt-3 text-xs text-muted" data-testid="breakdown-performance-note">
              These measure the return on the money put in, which needs a tracked
              opening balance. <Link to="/compare" className="underline hover:text-text">Compare</Link>{" "}
              can replay your contributions into another asset.
            </p>
          )}
          </>
        )}
      </Async>
    </Card>
  );
}

function AssetMix({ accounts, basis }) {
  if (!accounts.length) return null;
  return (
    <Card title="Asset class mix by portfolio" testId="breakdown-asset-mix">
      <div className="space-y-3">
        {accounts.map((account) => {
          const groups = groupByClass(account.items);
          if (!groups.length) return null;
          return (
            <Disclosure key={account.id} summary={`${account.name} · ${money(account.total, basis)}`} testId={`breakdown-mix-${account.id}`} open>
              <Donut data={groups} height={220} valueFormat={(v) => money(v, basis)} testId={`breakdown-donut-${account.id}`} />
            </Disclosure>
          );
        })}
      </div>
    </Card>
  );
}

export default function Family() {
  const { basis, accounts: portfolioAccounts } = usePortfolio();
  const state = useApi(() => valuation(null, basis), [basis], { pollMs: 60000 });

  return (
    <div>
      <PageHeader
        title="Portfolio Breakdown"
        subtitle="How each portfolio contributes to your combined net worth — current allocation, share over time, and performance."
      />
      <Async {...state} testId="breakdown-body">
        {(data) => {
          const rows = (data.accounts || []).sort((a, b) => Number(b.total) - Number(a.total));
          const grand = Number(data.total) || 0;
          // The server converts these figures and says which basis it managed to
          // apply, which is not always the one asked for -- with no USD rate it
          // answers in Toman. Trusting the request would label Toman as dollars.
          const rowBasis = data.basis || basis;
          if (!rows.length) {
            return <Empty testId="breakdown-empty">Add at least one portfolio to see the breakdown.</Empty>;
          }

          const shareDonut = rows.map((r) => ({ name: r.name, value: Number(r.total) || 0 }));

          return (
            <div className="space-y-5">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <StatTile label="Combined total" value={money(grand, rowBasis)} testId="breakdown-total" />
                <StatTile label="Portfolios" value={String(rows.length)} testId="breakdown-count" />
                <StatTile
                  label="Largest share"
                  value={rows[0] ? `${rows[0].name} · ${pct(grand ? Number(rows[0].total) / grand : 0)}` : "—"}
                  testId="breakdown-largest"
                />
              </div>

              <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
                <Card title="Current allocation" testId="breakdown-allocation">
                  <Donut data={shareDonut} valueFormat={(v) => money(v, rowBasis)} testId="breakdown-allocation-donut" />
                </Card>
                <Card title="Portfolio summary" testId="breakdown-table-card">
                  <Table
                    testId="breakdown-table"
                    rowKey={(r) => r.id}
                    rows={rows}
                    columns={[
                      { key: "name", header: "Portfolio", render: (r) => r.name },
                      { key: "total", header: "Net worth", align: "right", render: (r) => money(r.total, rowBasis) },
                      {
                        key: "share",
                        header: "Share",
                        align: "right",
                        render: (r) => pct(grand ? Number(r.total) / grand : 0),
                      },
                      {
                        key: "holdings",
                        header: "Holdings",
                        align: "right",
                        render: (r) => String((r.items || []).filter((i) => i.value != null).length),
                      },
                      {
                        key: "goal",
                        header: "Goal",
                        render: (r) => portfolioAccounts.find((a) => a.id === r.id)?.goal || "—",
                      },
                    ]}
                  />
                </Card>
              </div>

              <HistoryCharts accounts={rows} basis={basis} />
              <PerformanceTable accounts={rows} basis={basis} />
              <AssetMix accounts={rows} basis={rowBasis} />
            </div>
          );
        }}
      </Async>
    </div>
  );
}

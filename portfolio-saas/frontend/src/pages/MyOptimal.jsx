import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { useApi } from "../useApi.js";
import { myOptimal, frontier, listAssets } from "../api.js";
import { pct, num, signedPct, signedToman, humanize, date, assetLabel } from "../format.js";
import {
  Async,
  Badge,
  Button,
  Card,
  Disclosure,
  Empty,
  PageHeader,
  Delta,
  Table,
  Tabs,
} from "../components/ui.jsx";
import { GroupedBar, RiskScatter, STATUS_COLOR } from "../components/charts.jsx";

const SCENARIO_LABEL = { max_sharpe: "Max Sharpe", min_volatility: "Min Volatility" };

export default function MyOptimal() {
  const navigate = useNavigate();
  const { activeId } = usePortfolio();
  const [windowLabel, setWindowLabel] = useState("1Y");
  const [scenario, setScenario] = useState("max_sharpe");

  const optimalState = useApi(() => myOptimal(activeId), [activeId]);
  const frontierState = useApi(() => frontier(activeId), [activeId]);
  const assetsState = useApi(() => listAssets(), []);

  const label = useMemo(() => {
    const map = new Map((assetsState.data || []).map((a) => [a.key, a]));
    return (key) => assetLabel(map.get(key) || { key });
  }, [assetsState.data]);

  const { error } = optimalState;
  const noHoldings = error?.status === 400 && /no priced holdings/i.test(error.message || "");
  const universeTooSmall = error?.status === 503;

  return (
    <div>
      <PageHeader
        title="Optimal version of my portfolio"
        subtitle="Max-Sharpe and minimum-volatility allocations of the assets you already hold, over several historical lookback windows, next to how you actually performed."
      />

      {noHoldings ? (
        <Empty
          testId="optimal-empty-holdings"
          action={
            <Button variant="primary" onClick={() => navigate("/")}>
              Go to Portfolio
            </Button>
          }
        >
          Add holdings on the Portfolio page to see an optimized version of them.
        </Empty>
      ) : universeTooSmall ? (
        // UniverseTooSmall is a data-coverage state, not a fault — no red ErrorState.
        <Empty testId="optimal-empty-universe">{error.message}</Empty>
      ) : (
        <Async {...optimalState} testId="optimal-main" empty="No optimization data yet.">
          {(data) => (
            <MyOptimalBody
              data={data}
              frontierState={frontierState}
              label={label}
              windowLabel={windowLabel}
              setWindowLabel={setWindowLabel}
              scenario={scenario}
              setScenario={setScenario}
            />
          )}
        </Async>
      )}
    </div>
  );
}

function MyOptimalBody({ data, frontierState, label, windowLabel, setWindowLabel, scenario, setScenario }) {
  const windows = data.windows || [];
  const win = windows.find((w) => w.label === windowLabel) || windows[0];
  if (!win) return <Empty testId="optimal-empty-windows">No lookback windows available.</Empty>;

  const windowOptions = windows.map((w) => ({ value: w.label, label: w.label }));
  const scenarioOptions = [
    { value: "max_sharpe", label: "Max Sharpe", disabled: !win.max_sharpe },
    { value: "min_volatility", label: "Min Volatility", disabled: !win.min_volatility },
  ];
  // A stale tab selection (e.g. default "max_sharpe" on a window missing it) must
  // not blank modules 2-3 — fall back to whichever scenario this window actually has.
  const effectiveScenario = win[scenario] ? scenario : win.max_sharpe ? "max_sharpe" : "min_volatility";
  const opt = win[effectiveScenario] || null;

  const actual = win.actual || {};
  const am = actual.metrics || {};
  // The payload has no "actual return" field; recover it from the Sharpe identity:
  // sharpe = (return - risk_free) / vol  =>  return = sharpe * vol + risk_free.
  const actualReturn = am.sharpe * am.annualized_volatility + actual.risk_free_rate_annual;

  const comparisonRows = [
    {
      m: "Annualized return", fmt: pct, actual: actualReturn,
      ms: win.max_sharpe?.target_metrics?.expected_return_annual,
      mv: win.min_volatility?.target_metrics?.expected_return_annual,
    },
    {
      m: "Annualized volatility", fmt: pct, actual: am.annualized_volatility,
      ms: win.max_sharpe?.target_metrics?.annualized_volatility,
      mv: win.min_volatility?.target_metrics?.annualized_volatility,
    },
    {
      m: "Sharpe", fmt: (v) => num(v, 2), actual: am.sharpe,
      ms: win.max_sharpe?.target_metrics?.sharpe,
      mv: win.min_volatility?.target_metrics?.sharpe,
    },
    {
      m: "Max drawdown", fmt: pct, actual: am.max_drawdown,
      ms: win.max_sharpe?.diagnostics?.metrics?.max_drawdown,
      mv: win.min_volatility?.diagnostics?.metrics?.max_drawdown,
    },
  ];
  const comparisonColumns = [
    { key: "m", header: "Metric", render: (r) => r.m },
    { key: "actual", header: "Actual", align: "right", render: (r) => r.fmt(r.actual) },
    { key: "ms", header: "Max Sharpe", align: "right", render: (r) => r.fmt(r.ms) },
    { key: "mv", header: "Min Variance", align: "right", render: (r) => r.fmt(r.mv) },
  ];

  const currentW = opt?.current_weights || {};
  const targetW = opt?.target_weights || {};
  const allKeys = Array.from(new Set([...Object.keys(currentW), ...Object.keys(targetW)]));
  let barData = allKeys
    .map((k) => ({ key: k, name: label(k), a: currentW[k] || 0, b: targetW[k] || 0 }))
    .sort((x, y) => (targetW[y.key] || 0) - (targetW[x.key] || 0));
  if (barData.length > 12) {
    const rest = barData.slice(12);
    barData = [
      ...barData.slice(0, 12),
      { name: "Other", a: rest.reduce((s, e) => s + e.a, 0), b: rest.reduce((s, e) => s + e.b, 0) },
    ];
  }

  const tradeColumns = [
    { key: "asset", header: "Asset", render: (t) => label(t.key) },
    {
      key: "action", header: "Action",
      render: (t) => <Badge variant={t.action === "buy" ? "good" : "critical"}>{t.action.toUpperCase()}</Badge>,
    },
    { key: "current", header: "Current %", align: "right", render: (t) => pct(currentW[t.key]) },
    { key: "target", header: "Target %", align: "right", render: (t) => pct(targetW[t.key]) },
    {
      key: "delta_w", header: "Δ weight", align: "right",
      render: (t) => {
        // delta_weight_pct is the ONE non-fraction field in this payload (0-100,
        // not 0-1) and may be unsigned — force the sign from the trade action.
        const frac = (Math.abs(Number(t.delta_weight_pct)) / 100) * (t.action === "sell" ? -1 : 1);
        return <Delta value={frac} format={signedPct} />;
      },
    },
    {
      key: "delta_v", header: "Δ value", align: "right",
      render: (t) => (
        <Delta
          value={Number(t.delta_value_tomans) * (t.action === "buy" ? 1 : -1)}
          format={signedToman}
        />
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3">
        <Tabs options={windowOptions} value={win.label} onChange={setWindowLabel} label="Lookback window" testId="optimal-window-tabs" />
        <Tabs options={scenarioOptions} value={effectiveScenario} onChange={setScenario} label="Scenario" testId="optimal-scenario-tabs" />
      </div>

      {win.status === "insufficient_history" ? (
        <Empty testId="optimal-insufficient">{win.detail || "Not enough price history for this window."}</Empty>
      ) : (
        <>
          <Card
            title="Actual vs. optimized"
            subtitle={`How you performed over ${win.label} against two optimized alternatives.`}
            testId="optimal-comparison-card"
          >
            <Table columns={comparisonColumns} rows={comparisonRows} rowKey={(r) => r.m} testId="optimal-comparison" />
          </Card>

          <Card
            title="Current vs. target allocation"
            subtitle={`Your holdings against the ${SCENARIO_LABEL[effectiveScenario]} target.${opt?.cached ? " Cached result." : ""}`}
            testId="optimal-allocation-card"
          >
            {barData.length ? (
              <div data-testid="optimal-allocation-chart">
                <GroupedBar data={barData} labels={["Current", "Target"]} label="Current vs target allocation" />
              </div>
            ) : (
              <Empty testId="optimal-allocation-empty">No allocation data for this scenario.</Empty>
            )}
          </Card>

          <Card title="Rebalance trades" subtitle="Hypothetical trades to reach the target — not orders." testId="optimal-trades-card">
            <Table
              columns={tradeColumns}
              rows={opt?.rebalance_trades || []}
              rowKey={(t) => t.key}
              empty="The current allocation already matches this scenario."
              testId="optimal-trades"
            />
          </Card>

          {opt && (
            <Disclosure summary="Data window, assumptions, and exclusions" testId="optimal-disclosure">
              <p>
                {date(opt.data_window?.start)} – {date(opt.data_window?.end)} · {opt.observations} observations ·
                risk-free {pct(opt.risk_free_rate_annual)} · {humanize(opt.expected_return_method)}
              </p>
              {opt.excluded_assets?.length > 0 && (
                <ul className="mt-2 list-disc pl-4">
                  {opt.excluded_assets.map((e) => (
                    <li key={e.key}>{label(e.key)} — {humanize(e.reason)}</li>
                  ))}
                </ul>
              )}
              {opt.limitations?.map((l, i) => (
                <p key={`lim-${i}`} className="mt-1 text-muted">{l}</p>
              ))}
              {opt.degraded?.map((d, i) => (
                <p key={`deg-${i}`} className="mt-1 text-muted">{d}</p>
              ))}
            </Disclosure>
          )}
        </>
      )}

      <Async {...frontierState} testId="optimal-frontier" empty="No frontier data yet.">
        {(fdata) =>
          fdata.frontier?.length > 1 ? (
            <Card
              title="Efficient frontier"
              subtitle="Risk/return of random reweightings of your holdings, with your portfolio and the Max Sharpe target marked."
              testId="optimal-frontier-card"
            >
              <RiskScatter
                frontier={fdata.frontier.map((p) => ({ x: p.volatility, y: p.return }))}
                cloud={(fdata.cloud || []).map((p) => ({ x: p.volatility, y: p.return }))}
                points={[
                  fdata.current && {
                    name: "Your portfolio", x: fdata.current.volatility, y: fdata.current.return,
                    color: STATUS_COLOR.critical,
                  },
                  fdata.max_sharpe && {
                    name: "Max Sharpe",
                    x: fdata.max_sharpe.metrics.annualized_volatility,
                    y: fdata.max_sharpe.metrics.expected_return_annual,
                    color: STATUS_COLOR.good,
                  },
                ].filter(Boolean)}
              />
              <div className="mt-3 flex flex-wrap gap-4 text-xs text-muted">
                {fdata.current && (
                  <span className="inline-flex items-center gap-1.5">
                    <span aria-hidden="true" className="size-2.5 rounded-full" style={{ background: STATUS_COLOR.critical }} />
                    Your portfolio
                  </span>
                )}
                {fdata.max_sharpe && (
                  <span className="inline-flex items-center gap-1.5">
                    <span aria-hidden="true" className="size-2.5 rounded-full" style={{ background: STATUS_COLOR.good }} />
                    Max Sharpe
                  </span>
                )}
              </div>
              <p className="mt-2 text-xs text-muted">
                The grey cloud is random reweightings of the assets you already hold — it traces the risk/return
                range achievable without adding new assets.
              </p>
            </Card>
          ) : null
        }
      </Async>
    </div>
  );
}

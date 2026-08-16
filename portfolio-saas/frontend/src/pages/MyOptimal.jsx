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
import { DriftBars, GroupedBar, MoneyVsRisk, RiskScatter, STATUS_COLOR } from "../components/charts.jsx";

const SCENARIO_LABEL = {
  min_volatility: "Min Volatility",
  min_cvar: "Min Tail Risk",
  risk_parity: "Risk Parity",
  hrp: "Hierarchical Risk Parity",
  max_sharpe: "Max Sharpe",
};
// Ordered so the forecast-free scenarios come first. Max Sharpe is last because
// it is the only one whose answer depends on predicting returns, which one year
// of data cannot support.
const SCENARIO_ORDER = ["min_volatility", "min_cvar", "risk_parity", "hrp", "max_sharpe"];
// Mirrors MyOptimalView.WINDOWS; "Lifetime" has no fixed length, so the frontier
// falls back to the longest fixed window rather than guessing.
const WINDOW_DAYS = { "1Y": 365, "3Y": 1095, "5Y": 1825, Lifetime: 1825 };

export default function MyOptimal() {
  const navigate = useNavigate();
  const { activeId } = usePortfolio();
  const [windowLabel, setWindowLabel] = useState("1Y");
  // Defaults to the forecast-free scenario: it needs no return prediction, so
  // it is the one whose weights survive the fact that returns are unpredictable.
  const [scenario, setScenario] = useState("min_volatility");

  const optimalState = useApi(() => myOptimal(activeId), [activeId]);
  // The frontier follows the selected lookback, so the chart and the tables
  // above it describe the same window.
  const windowDays = WINDOW_DAYS[windowLabel] ?? 365;
  const frontierState = useApi(
    () => frontier(activeId, { window: windowDays }),
    [activeId, windowDays]
  );
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
  const scenarioOptions = SCENARIO_ORDER.map((key) => ({
    value: key,
    label: SCENARIO_LABEL[key],
    disabled: !win[key],
  }));
  // A stale tab selection (e.g. a scenario this window could not solve) must not
  // blank the page — fall back to the first scenario this window actually has,
  // in forecast-free-first order.
  const effectiveScenario = win[scenario]
    ? scenario
    : SCENARIO_ORDER.find((key) => win[key]) || "min_volatility";
  const opt = win[effectiveScenario] || null;

  const actual = win.actual || {};
  const am = actual.metrics || {};
  // `current_metrics` scores the current book on the optimizer's own mu/cov and
  // window, so this column is comparable with the two beside it. It replaces a
  // client-side reconstruction from the Sharpe identity, which was measured on a
  // different panel and produced NaN whenever sharpe was 0 or absent.
  const actualReturn = opt?.current_metrics?.expected_return_annual;

  const comparisonRows = [
    {
      m: "Annualized return", fmt: pct, actual: actualReturn,
      ms: win.max_sharpe?.target_metrics?.expected_return_annual,
      mv: win.min_volatility?.target_metrics?.expected_return_annual,
    },
    {
      m: "Annualized volatility", fmt: pct,
      actual: opt?.current_metrics?.annualized_volatility,
      ms: win.max_sharpe?.target_metrics?.annualized_volatility,
      mv: win.min_volatility?.target_metrics?.annualized_volatility,
    },
    {
      m: "Sharpe", fmt: (v) => num(v, 2), actual: opt?.current_metrics?.sharpe,
      ms: win.max_sharpe?.target_metrics?.sharpe,
      mv: win.min_volatility?.target_metrics?.sharpe,
    },
    {
      // Drawdown is path-dependent, so it comes from the diagnostics panel --
      // the optimizer's date-intersected window cannot produce it.
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

  // Asset-class roll-up: the per-asset bars answer "which holding", this answers
  // "how much of each kind of thing", which is the level most allocation
  // decisions are actually made at. Both dicts are fractions summing to ~1.
  const currentC = opt?.current_class_weights || {};
  const targetC = opt?.target_class_weights || {};
  const classData = Array.from(new Set([...Object.keys(currentC), ...Object.keys(targetC)]))
    .map((c) => ({ key: c, name: c, a: currentC[c] || 0, b: targetC[c] || 0 }))
    .sort((x, y) => y.b - x.b);

  const frozen = Object.entries(opt?.frozen_weights || {});

  // Diversification: current book vs. what the target would achieve. These are
  // the numbers that do not depend on predicting returns.
  const div = opt?.diversification || null;
  const divRows = div
    ? [
        {
          m: "Effective bets (risk-adjusted)",
          fmt: (v) => num(v, 2),
          cur: div.current?.effective_bets,
          tgt: div.target?.effective_bets,
        },
        {
          m: "Holdings (ignores correlation)",
          fmt: (v) => num(v, 2),
          cur: div.current?.effective_holdings,
          tgt: div.target?.effective_holdings,
        },
        {
          m: "Diversification ratio",
          fmt: (v) => num(v, 2),
          cur: div.current?.diversification_ratio,
          tgt: div.target?.diversification_ratio,
        },
      ]
    : [];
  const divColumns = [
    { key: "m", header: "Measure", render: (r) => r.m },
    { key: "cur", header: "Current", align: "right", render: (r) => r.fmt(r.cur) },
    { key: "tgt", header: "Target", align: "right", render: (r) => r.fmt(r.tgt) },
  ];

  // Risk contributions sum to 100%, so the gap against weight share reads
  // directly as "this holding carries more risk than its size suggests".
  const riskRows = (div?.current?.concentration_gap || []).map((r) => ({
    ...r,
    name: label(r.key),
  }));
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
          <div className="flex items-center gap-2" data-testid="optimal-forecast-badge">
            <Badge variant={opt?.forecast_free ? "good" : "warn"}>
              {opt?.forecast_free ? "No return forecast needed" : "Depends on a return forecast"}
            </Badge>
            <span className="text-xs text-muted">
              {opt?.forecast_free
                ? "Built from volatility and correlation only — the parts this much data can actually estimate."
                : "Ranks assets by expected return, which one year of data cannot pin down. Compare against the forecast-free scenarios."}
            </span>
          </div>

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
              <div data-testid="optimal-allocation-chart" className="space-y-6">
                <DriftBars
                  rows={barData.map((r) => ({ key: r.name, current: r.a, target: r.b }))}
                  label="Drift from target weight"
                  testId="optimal-drift"
                />
                <GroupedBar data={barData} labels={["Current", "Target"]} label="Current vs target allocation" />
              </div>
            ) : (
              <Empty testId="optimal-allocation-empty">No allocation data for this scenario.</Empty>
            )}
          </Card>

          <Card
            title="Current vs. target by asset class"
            subtitle="The same target rolled up to asset classes."
            testId="optimal-class-card"
          >
            {classData.length ? (
              <div data-testid="optimal-class-chart">
                <GroupedBar data={classData} labels={["Current", "Target"]} label="Current vs target allocation by asset class" />
              </div>
            ) : (
              <Empty testId="optimal-class-empty">No asset-class data for this scenario.</Empty>
            )}
          </Card>

          {frozen.length > 0 && (
            <Card
              title="Held at current weight"
              subtitle="Not enough price history to optimize these — they are kept as-is, not sold."
              testId="optimal-frozen-card"
            >
              <ul className="list-disc pl-4 text-sm">
                {frozen.map(([key, info]) => (
                  <li key={key}>
                    {label(key)} — {pct(info.weight)} · {humanize(info.reason)}
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-muted">
                The optimizer allocated the remaining {pct(opt?.optimized_share)} of the portfolio.
              </p>
            </Card>
          )}

          {div && (
            <Card
              title="Diversification"
              subtitle="How many genuinely independent bets you hold — the part of this page that needs no forecast."
              testId="optimal-diversification-card"
            >
              <Table
                columns={divColumns}
                rows={divRows}
                rowKey={(r) => r.m}
                testId="optimal-diversification"
              />
              <p className="mt-3 text-xs text-muted">
                Effective bets counts positions after correlation: assets that move together
                collapse into one. A diversification ratio of 1.0 means nothing cancels out.
              </p>
            </Card>
          )}

          {riskRows.length > 0 && (
            <Card
              title="Where your risk actually comes from"
              subtitle="Share of portfolio risk vs. share of money. A large gap is a position doing more than it looks."
              testId="optimal-risk-card"
            >
              <MoneyVsRisk
                rows={div.current.concentration_gap}
                testId="optimal-risk-contributions"
              />
            </Card>
          )}

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
                {opt.basis ? <> · {humanize(opt.basis)}</> : null}
              </p>
              {opt.expected_return_provenance?.mean_standard_error != null && (
                <p className="mt-1 text-muted">
                  Expected-return uncertainty: ±{pct(opt.expected_return_provenance.mean_standard_error)} per
                  year on {num(opt.expected_return_provenance.sample_years, 1)} year(s) of data. This is the
                  honest error bar on any return forecast built from this window.
                </p>
              )}
              {opt.excluded_assets?.length > 0 && (
                <ul className="mt-2 list-disc pl-4">
                  {opt.excluded_assets.map((e) => (
                    <li key={e.key}>{label(e.key)} — {humanize(e.reason)}</li>
                  ))}
                </ul>
              )}
              {Object.keys(opt.proxy_groups || {}).length > 0 && (
                <div className="mt-2">
                  <p className="font-medium">Measured against a stand-in price series</p>
                  <ul className="mt-1 list-disc pl-4">
                    {Object.entries(opt.proxy_groups).map(([proxy, members]) => (
                      <li key={proxy}>
                        {members.map((k) => label(k)).join(", ")} — risk measured from {label(proxy)}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {opt.constraints_floored?.length > 0 && (
                <div className="mt-2">
                  <p className="font-medium">Caps raised to fit your current portfolio</p>
                  <ul className="mt-1 list-disc pl-4">
                    {opt.constraints_floored.map((c) => (
                      <li key={c.cap}>
                        {humanize(c.cap)} — policy {pct(c.policy)}, raised to {pct(c.floored_to)}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {opt.sleeves?.length > 0 && (
                <div className="mt-2">
                  <p className="font-medium">Hard-asset sleeves (combined cap enforced)</p>
                  <ul className="mt-1 list-disc pl-4">
                    {opt.sleeves.map((s) => (
                      <li key={s.id || s.classes?.join("-")}>
                        {s.label || (s.classes || []).join(" + ")}
                        {s.assets?.length > 0 && (
                          <> — {s.assets.map((k) => label(k)).join(", ")}</>
                        )}
                        {" · "}
                        max {pct(s.max_combined_weight)} combined
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {opt.correlation_clusters?.length > 0 && (
                <div className="mt-2">
                  <p className="font-medium">Correlated asset groups (combined cap enforced)</p>
                  <ul className="mt-1 list-disc pl-4">
                    {opt.correlation_clusters.map((c) => (
                      <li key={c.assets.join("-")}>
                        {c.assets.map((k) => label(k)).join(", ")}
                        {c.avg_pairwise_correlation != null && (
                          <> · avg r {num(c.avg_pairwise_correlation, 2)}</>
                        )}
                        {c.min_pairwise_correlation != null && (
                          <> · weakest pair r {num(c.min_pairwise_correlation, 2)}</>
                        )}
                        {" · "}
                        max {pct(c.max_combined_weight)} combined
                      </li>
                    ))}
                  </ul>
                </div>
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

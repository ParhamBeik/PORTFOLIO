import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { useApi } from "../useApi.js";
import { myOptimal, frontier, listAssets, robustness } from "../api.js";
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
  Select,
  Table,
  Tabs,
} from "../components/ui.jsx";
import { DriftBars, GroupedBar, MoneyVsRisk, RiskScatter, STATUS_COLOR } from "../components/charts.jsx";

const SCENARIO_LABEL = {
  efficient_risk: "My Risk Budget",
  min_volatility: "Min Volatility",
  min_cvar: "Min Tail Risk",
  risk_parity: "Risk Parity",
  hrp: "Hierarchical Risk Parity",
  max_sharpe: "Max Sharpe",
};
// `efficient_risk` leads when it exists, because the user asked for it by name.
// The rest are ordered forecast-free first; Max Sharpe is last because it is the
// only one whose answer depends on predicting returns, which one year of data
// cannot support.
const SCENARIO_ORDER = ["efficient_risk", "min_volatility", "min_cvar", "risk_parity", "hrp", "max_sharpe"];
// Mirrors MyOptimalView.WINDOWS; "Lifetime" has no fixed length, so the frontier
// falls back to the longest fixed window rather than guessing.
const WINDOW_DAYS = { "1Y": 365, "3Y": 1095, "5Y": 1825, Lifetime: 1825 };
// "" means no cap. Mirrors MIN_CARDINALITY (3) and MAX_ASSETS_CEILING (40) on
// the server, which rejects anything outside that range with a 400.
const MAX_ASSET_OPTIONS = ["", 3, 5, 8, 10, 15, 20];
// Risk tolerance as annualized volatility. "" means "no ceiling -- show me the
// standing scenarios"; the server accepts 0.01 to 2.0.
const RISK_OPTIONS = [
  ["", "No risk ceiling"],
  ["0.10", "Cautious — up to 10%/yr"],
  ["0.20", "Balanced — up to 20%/yr"],
  ["0.35", "Growth — up to 35%/yr"],
  ["0.50", "Aggressive — up to 50%/yr"],
];

export default function MyOptimal() {
  const navigate = useNavigate();
  const { activeId } = usePortfolio();
  const [windowLabel, setWindowLabel] = useState("1Y");
  // Defaults to the forecast-free scenario: it needs no return prediction, so
  // it is the one whose weights survive the fact that returns are unpredictable.
  const [scenario, setScenario] = useState("min_volatility");
  // "Show me the best portfolio using at most N of my assets." Empty = no cap,
  // which is what the solvers produce on their own.
  const [maxAssets, setMaxAssets] = useState("");
  // Risk tolerance stated as a number rather than implied by a scenario tab.
  const [riskCeiling, setRiskCeiling] = useState("");

  const optimalState = useApi(
    () =>
      myOptimal(activeId, {
        maxAssets: maxAssets === "" ? null : Number(maxAssets),
        targetVolatility: riskCeiling === "" ? null : Number(riskCeiling),
      }),
    [activeId, maxAssets, riskCeiling]
  );
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
              maxAssets={maxAssets}
              setMaxAssets={setMaxAssets}
              riskCeiling={riskCeiling}
              setRiskCeiling={setRiskCeiling}
            />
          )}
        </Async>
      )}
    </div>
  );
}

function MyOptimalBody({
  data, frontierState, label, windowLabel, setWindowLabel, scenario, setScenario,
  maxAssets, setMaxAssets, riskCeiling, setRiskCeiling,
}) {
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

  // The position cap is a preference, not an invariant: the caps can make a
  // small portfolio impossible to fully invest, and "hold at most N" is a
  // greedy relaxation of a mixed-integer constraint. Both facts belong on the
  // screen — an answer that quietly ignored the cap reads as a bug.
  const cardinality = opt?.cardinality || null;
  const capUnhonoured = (opt?.degraded || []).includes("cardinality_infeasible");
  const cardinalityNote = !cardinality
    ? ""
    : capUnhonoured
      ? `The per-asset and per-class limits cannot fully invest this portfolio in only ${cardinality.requested} holdings, so the cap was not applied.`
      : cardinality.method === "not_applicable"
        ? "Equal weight spreads across everything, so a position cap has nothing to rank."
        : `Showing the best ${cardinality.applied} of your holdings: the largest positions were kept and re-optimized among themselves. A different set of that size could score slightly better.`;

  // A ceiling under the minimum-variance floor is unreachable by any weights.
  // The scenario answers with that floor rather than failing, so the screen has
  // to say the number it is showing is not the number that was asked for.
  const riskTarget = opt?.risk_target || null;
  const riskNote = !riskTarget
    ? ""
    : riskTarget.met
      ? `Earning the most available inside your ${pct(riskTarget.requested)} ceiling; this allocation runs at ${pct(riskTarget.achieved)}.`
      : `No combination of your assets is as calm as ${pct(riskTarget.requested)}. This is the least volatile portfolio they can build, at ${pct(riskTarget.achieved)}.`;

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
    {
      // Two bare columns under a page called "My Optimal" read as "the right
      // column is the better one". On this account the target holds FEWER
      // effective bets than the book it is recommending replaced (1.84 vs 1.99),
      // and nothing on screen said so. Higher is more diversified on all three.
      key: "delta",
      header: "Change",
      align: "right",
      render: (r) => {
        if (r.cur == null || r.tgt == null) return "—";
        const d = Number(r.tgt) - Number(r.cur);
        if (Math.abs(d) < 0.005) return <span className="text-muted">no change</span>;
        return (
          <span className={d > 0 ? "text-good" : "text-critical"}>
            {d > 0 ? "+" : ""}{num(d, 2)} {d > 0 ? "more spread" : "more concentrated"}
          </span>
        );
      },
    },
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
    // Absent from these maps means "none of it", which is 0% -- the same answer
    // an explicit zero gives. Passing `undefined` to `pct` printed "—" on some
    // rows and "0%" on others for one meaning, beside a SELL badge and a
    // negative delta that had both already assumed zero.
    { key: "current", header: "Current %", align: "right", render: (t) => pct(currentW[t.key] ?? 0) },
    { key: "target", header: "Target %", align: "right", render: (t) => pct(targetW[t.key] ?? 0) },
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
        <Select
          label="Risk tolerance"
          value={riskCeiling}
          onChange={(e) => {
            // Picking a ceiling is asking for the scenario it produces; leaving
            // the tab on Min Volatility would answer a question nobody asked.
            setRiskCeiling(e.target.value);
            setScenario(e.target.value === "" ? "min_volatility" : "efficient_risk");
          }}
          data-testid="optimal-risk-ceiling"
        >
          {RISK_OPTIONS.map(([value, text]) => (
            <option key={value || "none"} value={value}>{text}</option>
          ))}
        </Select>
        <Select
          label="Maximum number of positions"
          value={maxAssets}
          onChange={(e) => setMaxAssets(e.target.value)}
          data-testid="optimal-max-assets"
        >
          {MAX_ASSET_OPTIONS.map((n) => (
            <option key={n === "" ? "all" : n} value={n}>
              {n === "" ? "Any number of holdings" : `At most ${n} holdings`}
            </option>
          ))}
        </Select>
      </div>

      {riskTarget ? (
        <p
          className={`text-xs ${riskTarget.met ? "text-muted" : "text-warn"}`}
          data-testid="optimal-risk-note"
        >
          {riskNote}
        </p>
      ) : null}

      {cardinality ? (
        <p className="text-xs text-muted" data-testid="optimal-cardinality-note">
          {cardinalityNote}
        </p>
      ) : null}

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
              <Disclosure summary="How to read these" testId="optimal-diversification-help">
                <p className="text-sm">
                  Effective bets counts positions after correlation: assets that move together
                  collapse into one. A diversification ratio of 1.0 means nothing cancels out.
                  Higher is more spread out on all three rows.
                </p>
                <p className="mt-2 text-sm">
                  These describe the target — they are not what it was chosen for. The optimiser
                  maximises return for the risk you allow, so it can and does concentrate when
                  concentrating pays. A "more concentrated" row is that trade being made, not a
                  mistake; it is worth asking whether you want it.
                </p>
              </Disclosure>
            </Card>
          )}

          <RobustnessPanel
            scenario={effectiveScenario}
            windowDays={WINDOW_DAYS[win.label] ?? 365}
            riskCeiling={riskCeiling}
            label={label}
          />

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
                {opt.risk_free_rate_source ? (
                  <span className="block text-muted">Risk-free rate: {opt.risk_free_rate_source}</span>
                ) : null}
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
              <Disclosure summary="What the cloud shows" testId="optimal-frontier-help">
                <p className="text-sm">
                  The grey cloud is random reweightings of the assets you already hold — it traces the risk/return
                  range achievable without adding new assets.
                </p>
              </Disclosure>
            </Card>
          ) : null
        }
      </Async>
    </div>
  );
}


/**
 * "How much of this allocation is signal?"
 *
 * The optimizer returns one point estimate, and a point estimate from ~250
 * observations is mostly luck. This bootstraps the panel 200 times and reports
 * the 5th-95th percentile each weight lands in. A position whose band spans
 * 10%-70% was never really chosen -- it won a coin toss -- which is why the
 * table sorts by band WIDTH rather than by weight: the least trustworthy rows
 * are the ones worth reading.
 *
 * Gated behind a button and given its own timeout because 200 re-solves cannot
 * ride along with a page load that already does eight.
 */
function RobustnessPanel({ scenario, windowDays, riskCeiling, label }) {
  const { activeId } = usePortfolio();
  const [requested, setRequested] = useState(false);
  const state = useApi(
    () =>
      robustness(activeId, {
        scenario,
        window: windowDays,
        // Only efficient_risk reads it, and it cannot solve without it.
        targetVolatility: riskCeiling === "" ? null : Number(riskCeiling),
      }),
    [activeId, scenario, windowDays, riskCeiling],
    { enabled: requested, timeoutMs: 90000 }
  );

  return (
    <Card
      title="How much of this is signal?"
      subtitle="Re-solves the allocation on 200 bootstrap resamples of the same window. A wide band means the weight was luck, not a decision."
      testId="optimal-robustness-card"
      actions={
        requested ? null : (
          <Button variant="primary" onClick={() => setRequested(true)} data-testid="optimal-robustness-run">
            Run 200 re-solves
          </Button>
        )
      }
    >
      {!requested ? (
        <p className="text-sm text-muted">
          This takes a few seconds — it solves the portfolio 200 more times, so it is not run with the page.
        </p>
      ) : (
        <Async {...state} testId="optimal-robustness" empty="No resampling result." minHeight={200}>
          {(data) => {
            const bands = data?.robustness?.bands || {};
            const robust = data?.robustness?.weights || {};
            const point = data?.target_weights || {};
            const rows = Object.entries(bands)
              .map(([key, b]) => ({ key, point: point[key] || 0, robust: robust[key] || 0, ...b }))
              .filter((r) => r.p95 > 1e-6 || r.point > 1e-6)
              .sort((a, b) => b.width - a.width);
            const converged = data?.robustness?.converged ?? 0;
            if (!rows.length) {
              return <Empty testId="optimal-robustness-empty">Not enough shared history to resample this window.</Empty>;
            }
            return (
              <>
                <Table
                  testId="optimal-robustness-table"
                  rowKey={(r) => r.key}
                  rows={rows}
                  columns={[
                    { key: "asset", header: "Asset", render: (r) => label(r.key) },
                    { key: "point", header: "This page's weight", align: "right", render: (r) => pct(r.point) },
                    { key: "robust", header: "Averaged over draws", align: "right", render: (r) => pct(r.robust) },
                    { key: "band", header: "5th–95th percentile", align: "right", render: (r) => `${pct(r.p05)} – ${pct(r.p95)}` },
                    {
                      key: "width",
                      header: "Band width",
                      align: "right",
                      // A band wider than 30 percentage points is wider than most
                      // allocation decisions people argue about, so it is the point
                      // at which the number stops carrying information.
                      render: (r) => (
                        <Badge variant={r.width > 0.3 ? "warn" : r.width > 0.15 ? "neutral" : "good"}>
                          {pct(r.width)}
                        </Badge>
                      ),
                    },
                  ]}
                />
                <p className="mt-2 text-xs text-muted">
                  {converged} of {data?.robustness?.n_draws ?? 0} resamples solved. Draws that could not solve are
                  dropped rather than counted, so a thin sample cannot pass for a confident band.
                </p>
              </>
            );
          }}
        </Async>
      )}
    </Card>
  );
}

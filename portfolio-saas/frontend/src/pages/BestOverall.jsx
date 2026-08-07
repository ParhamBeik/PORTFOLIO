import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { bestOverall, listAssets, valuation } from "../api.js";
import { useApi } from "../useApi.js";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import {
  Async,
  Badge,
  Card,
  Delta,
  Disclosure,
  Empty,
  PageHeader,
  StatTile,
  Table,
  Tabs,
} from "../components/ui.jsx";
import { Donut, GroupedBar } from "../components/charts.jsx";
import {
  assetLabel,
  date,
  dateTime,
  humanize,
  num,
  pct,
  signedPct,
  signedToman,
} from "../format.js";

const SCENARIOS = [
  { value: "max_sharpe", label: "Max Sharpe" },
  { value: "min_volatility", label: "Min volatility" },
];
const HOLD_BAND = 0.005; // |delta weight| under 0.5pp reads as HOLD, not a trade signal.

function actionBadge(deltaWeight) {
  if (Math.abs(deltaWeight) < HOLD_BAND) return <Badge variant="neutral">HOLD</Badge>;
  return deltaWeight > 0 ? <Badge variant="good">BUY</Badge> : <Badge variant="critical">SELL</Badge>;
}

export default function BestOverall() {
  const { activeId, basis } = usePortfolio();
  const best = useApi(bestOverall, []);
  const val = useApi(() => valuation(activeId, basis), [activeId, basis]);
  const assets = useApi(listAssets, []);
  const labelFor = useMemo(() => {
    const map = new Map((assets.data || []).map((a) => [a.key, assetLabel(a)]));
    return (key) => map.get(key) || key;
  }, [assets.data]);

  return (
    <div>
      <PageHeader
        title="Best portfolio overall"
        subtitle="The optimal allocation across every tracked asset — not just what you hold. Computed nightly from the full price warehouse."
      />
      <Async {...best} testId="universe-best">
        {(data) => <BestOverallBody data={data} valState={val} labelFor={labelFor} />}
      </Async>
    </div>
  );
}

function BestOverallBody({ data, valState, labelFor }) {
  const windows = data.windows || [];
  const allEmpty = windows.every((w) => w.max_sharpe == null && w.min_volatility == null);

  // The nightly Celery job hasn't produced a snapshot yet — distinct from a
  // per-window "not enough history", which is a normal, expected state below.
  if (!data.as_of || allEmpty) {
    return (
      <Empty testId="universe-not-computed">
        The nightly optimization has not run yet. This page fills in once the first snapshot is computed.
      </Empty>
    );
  }

  return (
    <BestOverallReady data={data} windows={windows} valState={valState} labelFor={labelFor} />
  );
}

function BestOverallReady({ data, windows, valState, labelFor }) {
  const [windowLabel, setWindowLabel] = useState(
    () => (windows.find((w) => w.label === "1Y") ? "1Y" : windows[0]?.label)
  );
  const [scenario, setScenario] = useState("max_sharpe");

  const win = windows.find((w) => w.label === windowLabel) ?? windows[0];
  // Falls back to whichever scenario the selected window actually has, so
  // switching windows never strands the tabs on a now-disabled selection.
  const effectiveScenario =
    win?.[scenario] != null ? scenario : SCENARIOS.map((s) => s.value).find((v) => win?.[v] != null) ?? scenario;
  const opt = win?.[effectiveScenario];

  const windowOptions = windows.map((w) => ({ value: w.label, label: w.label }));
  const scenarioOptions = SCENARIOS.map((s) => ({ ...s, disabled: win?.[s.value] == null }));

  const weights = useMemo(
    () => (opt ? Object.entries(opt.target_weights).sort((a, b) => b[1] - a[1]) : []),
    [opt]
  );
  const donutData = useMemo(() => {
    const top = weights.slice(0, 7).map(([k, v]) => ({ name: labelFor(k), value: v }));
    const otherSum = weights.slice(7).reduce((s, [, v]) => s + v, 0);
    return otherSum > 0 ? [...top, { name: "Other", value: otherSum }] : top;
  }, [weights, labelFor]);

  return (
    <>
      <p className="mt-1 text-sm text-muted">Computed {dateTime(data.as_of)}</p>

      <div className="mt-6 flex flex-wrap gap-4">
        <Tabs
          testId="universe-window"
          label="Lookback window"
          options={windowOptions}
          value={windowLabel}
          onChange={setWindowLabel}
        />
        <Tabs
          testId="universe-scenario"
          label="Scenario"
          options={scenarioOptions}
          value={effectiveScenario}
          onChange={setScenario}
        />
      </div>

      {!opt ? (
        <Empty testId="universe-window-empty">
          Not enough price history for the {windowLabel} window yet.
        </Empty>
      ) : (
        <>
          <div className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <StatTile
              testId="universe-return"
              label="Expected annual return"
              value={pct(opt.target_metrics.expected_return_annual)}
            />
            <StatTile
              testId="universe-volatility"
              label="Annualized volatility"
              value={pct(opt.target_metrics.annualized_volatility)}
            />
            <StatTile
              testId="universe-sharpe"
              label="Sharpe"
              value={num(opt.target_metrics.sharpe, 2)}
            />
          </div>

          <Card
            testId="universe-weights-card"
            className="mt-6"
            title="Target weights"
            subtitle={`${SCENARIOS.find((s) => s.value === effectiveScenario)?.label} allocation for the ${windowLabel} window.`}
          >
            <Donut data={donutData} valueFormat={pct} testId="universe-weights-donut" label="Target allocation" />
            <div className="mt-4">
              <Table
                testId="universe-weights-table"
                columns={[
                  { key: "asset", header: "Asset", render: (r) => r.label },
                  { key: "weight", header: "Weight", align: "right", render: (r) => pct(r.weight) },
                ]}
                rows={weights.map(([key, weight]) => ({ key, label: labelFor(key), weight }))}
                rowKey={(r) => r.key}
              />
            </div>
          </Card>

          <Card
            testId="universe-gap-card"
            className="mt-6"
            title="Gap vs my portfolio"
            subtitle="Computed in the browser by differencing your latest valuation against the ideal weights above. Indicative only — places no orders, and ignores transaction costs, liquidity, and tax."
          >
            <Async {...valState} testId="universe-gap" empty="No valuation yet.">
              {(v) => <GapModule valuation={v} target={opt.target_weights} labelFor={labelFor} />}
            </Async>
          </Card>
        </>
      )}

      <LeadersModule data={data} />

      {opt && <AssumptionsDisclosure opt={opt} labelFor={labelFor} />}
    </>
  );
}

function GapModule({ valuation: v, target, labelFor }) {
  const total = Number(v.total) || 0;
  if (!v.items?.length || total <= 0) {
    return (
      <Empty
        testId="universe-gap-empty"
        action={
          <Link
            to="/"
            className="inline-block rounded-md border border-transparent bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
          >
            Go to Portfolio
          </Link>
        }
      >
        Add holdings to see your gap to the ideal allocation.
      </Empty>
    );
  }

  const currentByKey = new Map(v.items.map((i) => [i.key, Number(i.value) / total]));
  const keys = new Set([...currentByKey.keys(), ...Object.keys(target)]);
  const rows = [...keys]
    .map((key) => {
      const current = currentByKey.get(key) ?? 0;
      const targetWeight = target[key] ?? 0;
      const deltaWeight = targetWeight - current;
      return { key, label: labelFor(key), current, target: targetWeight, deltaWeight, deltaValue: deltaWeight * total };
    })
    .sort((a, b) => Math.abs(b.deltaValue) - Math.abs(a.deltaValue));

  const byTarget = [...rows].sort((a, b) => b.target - a.target);
  const top12 = byTarget.slice(0, 12);
  const rest = byTarget.slice(12);
  const chartData = top12.map((r) => ({ name: r.label, a: r.current, b: r.target }));
  if (rest.length) {
    chartData.push({
      name: "Other",
      a: rest.reduce((s, r) => s + r.current, 0),
      b: rest.reduce((s, r) => s + r.target, 0),
    });
  }

  return (
    <>
      <div className="mt-2" data-testid="universe-gap-chart">
        <GroupedBar data={chartData} labels={["Yours", "Ideal"]} label="Your weights versus the ideal weights" />
      </div>
      <div className="mt-4">
        <Table
          testId="universe-gap-table"
          columns={[
            { key: "asset", header: "Asset", render: (r) => r.label },
            { key: "action", header: "Action", render: (r) => actionBadge(r.deltaWeight) },
            { key: "yours", header: "Yours", align: "right", render: (r) => pct(r.current) },
            { key: "ideal", header: "Ideal", align: "right", render: (r) => pct(r.target) },
            {
              key: "dw",
              header: "Δ weight",
              align: "right",
              render: (r) => <Delta value={r.deltaWeight} format={signedPct} />,
            },
            {
              key: "dv",
              header: "Δ value",
              align: "right",
              render: (r) => <Delta value={r.deltaValue} format={signedToman} />,
            },
          ]}
          rows={rows}
          rowKey={(r) => r.key}
        />
      </div>
    </>
  );
}

function LeadersModule({ data }) {
  const classes = Object.entries(data.leaders || {});
  return (
    <Card
      testId="universe-leaders-card"
      className="mt-6"
      title="Top performers by asset class (trailing 1 year)"
      subtitle="Independent of the window tabs above — leaders are always ranked on the trailing 1-year window."
    >
      {data.leaders_as_of && (
        <p className="text-xs text-muted">Leaders as of {date(data.leaders_as_of)}.</p>
      )}
      {classes.length === 0 ? (
        <Empty testId="universe-leaders-empty">No leader data yet.</Empty>
      ) : (
        classes.map(([cls, rows]) => (
          <div key={cls} className="mt-4 first:mt-2">
            <h3 className="text-sm font-semibold">{humanize(cls)}</h3>
            <Table
              testId={`universe-leaders-${cls}`}
              columns={[
                { key: "symbol", header: "Symbol", render: (r) => r.name || r.symbol },
                { key: "sharpe", header: "Sharpe", align: "right", render: (r) => num(r.sharpe, 2) },
                { key: "sortino", header: "Sortino", align: "right", render: (r) => num(r.sortino, 2) },
                { key: "return", header: "Return", align: "right", render: (r) => pct(r.expected_return_annual) },
                { key: "vol", header: "Volatility", align: "right", render: (r) => pct(r.volatility_annual) },
              ]}
              rows={rows.slice(0, 5)}
              rowKey={(r) => r.symbol}
            />
          </div>
        ))
      )}
    </Card>
  );
}

function AssumptionsDisclosure({ opt, labelFor }) {
  return (
    <Disclosure testId="universe-disclosure" summary="Data window, assumptions, and exclusions">
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
        <div>
          <dt className="text-xs tracking-wide text-muted uppercase">Data window</dt>
          <dd>{date(opt.data_window?.start)} – {date(opt.data_window?.end)}</dd>
        </div>
        <div>
          <dt className="text-xs tracking-wide text-muted uppercase">Observations</dt>
          <dd>{num(opt.observations, 0)}</dd>
        </div>
        <div>
          <dt className="text-xs tracking-wide text-muted uppercase">Risk-free rate</dt>
          <dd>{pct(opt.risk_free_rate_annual)}</dd>
        </div>
        <div>
          <dt className="text-xs tracking-wide text-muted uppercase">Expected return method</dt>
          <dd>{humanize(opt.expected_return_method)}</dd>
        </div>
      </dl>
      {opt.excluded_assets?.length > 0 && (
        <div className="mt-3">
          <div className="text-xs tracking-wide text-muted uppercase">Excluded assets</div>
          <ul className="mt-1 list-disc pl-4">
            {opt.excluded_assets.map((e) => (
              <li key={e.key}>{labelFor(e.key)} — {humanize(e.reason)}</li>
            ))}
          </ul>
        </div>
      )}
      {opt.limitations?.length > 0 && (
        <div className="mt-3">
          <div className="text-xs tracking-wide text-muted uppercase">Limitations</div>
          <ul className="mt-1 list-disc pl-4">
            {opt.limitations.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </div>
      )}
    </Disclosure>
  );
}

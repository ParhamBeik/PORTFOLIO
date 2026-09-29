import { frontier } from "../api.js";
import { RiskScatter } from "../components/charts.jsx";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { Async, Card, Empty } from "../components/ui.jsx";
import { pct } from "../format.js";
import { useApi } from "../useApi.js";

export default function Risk() {
  const { activeId } = usePortfolio();
  const state = useApi(() => frontier(activeId, { window: 365 }), [activeId]);
  return <Card title="Risk and possible reweightings" subtitle="A diagnostic view of eligible holdings over the past year, not an allocation recommendation." testId="portfolio-risk">
    <Async {...state} testId="portfolio-risk-body">
      {(data) => {
        const exclusions = data.excluded_assets || [];
        const coverage = data.covered_share ?? 0;
        const complete = coverage >= 0.999 && exclusions.length === 0 && !(data.warnings || []).length && !data.incomplete_sessions;
        return <div className="space-y-3 text-sm">
          <p>Value covered by eligible price history: <strong>{pct(coverage)}</strong>. {data.history_observations || 0} return observations.</p>
          {exclusions.length > 0 && <p className="text-muted">Excluded: {exclusions.map((item) => `${item.key} (${item.reason})`).join(", ")}.</p>}
          {(data.warnings || []).length > 0 && <p className="text-muted">History uncertainty: {data.warnings.map((item) => `${item.key}: ${item.reason}`).join(", ")}.</p>}
          {data.incomplete_sessions > 0 && <p className="text-muted">{data.incomplete_sessions} sessions have gaps in the aligned return history.</p>}
          {complete && data.frontier?.length > 1 && data.current ? <RiskScatter
            frontier={data.frontier.map((point) => ({ x: point.volatility, y: point.return }))}
            cloud={(data.cloud || []).map((point) => ({ x: point.volatility, y: point.return }))}
            points={[{ name: "Current portfolio", x: data.current.volatility, y: data.current.return }]}
            label="Current portfolio against possible reweightings of its eligible holdings"
          /> : <Empty>Risk metrics are withheld until the held assets have enough verified, aligned history.</Empty>}
          <p className="text-xs text-muted">Historical estimates are uncertain. The cloud represents possible weights of assets already held; it does not predict future returns.</p>
        </div>;
      }}
    </Async>
  </Card>;
}

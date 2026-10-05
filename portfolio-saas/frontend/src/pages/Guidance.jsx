import { useMemo, useState } from "react";
import { translate } from "../i18n.js";
import { guidance, listAssets, setRiskProfile } from "../api.js";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { Async, Badge, Card, Empty, PageHeader, Select } from "../components/ui.jsx";
import { assetLabel, pct } from "../format.js";
import { useApi } from "../useApi.js";

const PROFILE_OPTIONS = [
  ["conservative", "Conservative"],
  ["balanced", "Balanced"],
  ["growth", "Growth"],
];

function Allocation({ title, weights, labelFor, testId }) {
  const rows = Object.entries(weights || {}).sort((a, b) => b[1] - a[1]);
  if (!rows.length) return <Empty testId={`${testId}-empty`}>Not enough verified history yet.</Empty>;
  return (
    <Card title={title}>
      <ul className="space-y-2" data-testid={testId}>
        {rows.map(([key, weight]) => (
          <li key={key} className="flex justify-between gap-4 border-b border-border py-1 text-sm">
            <span>{labelFor(key)}</span><span className="tabular">{pct(weight)}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export default function Guidance({ user, onUserChange }) {
  const { activeId } = usePortfolio();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const result = useApi(() => guidance(activeId), [activeId, user.risk_profile], { timeoutMs: 120000 });
  const assets = useApi(listAssets, []);
  const labelFor = useMemo(() => {
    const names = new Map((assets.data || []).map((asset) => [asset.key, assetLabel(asset)]));
    return (key) => names.get(key) || key;
  }, [assets.data]);

  const changeProfile = async (event) => {
    setSaving(true);
    setSaveError("");
    try {
      onUserChange(await setRiskProfile(event.target.value));
    } catch (error) {
      setSaveError(error.message || "Could not save your risk profile.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <PageHeader
        title="Risk"
        subtitle="Rebalance models whose inputs and formulas are still being audited. They have not passed the source checks the rest of the app is held to; holdings and performance on Home are verified."
        meta={(
          <p className="mt-1 flex items-center gap-2 text-sm text-muted" role="note" data-testid="guidance-beta">
            <Badge variant="warn">Beta</Badge>
            {translate("Not yet source-verified — a starting point, not advice.")}
          </p>
        )}
      />
      <div className="mb-5 max-w-xs">
        <Select label="Risk profile" value={user.risk_profile || "balanced"} onChange={changeProfile} disabled={saving} data-testid="guidance-risk-profile">
          {PROFILE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{translate(label)}</option>)}
        </Select>
        {saveError && <p role="alert" className="text-sm text-[var(--c-warn-text)]">{saveError}</p>}
      </div>
      <Async {...result} testId="guidance-result">
        {(data) => (
          <div className="space-y-5">
            <p className="text-sm text-muted" data-testid="guidance-window">
              {translate("Personal:")} {data.personal_window_days ? translate(data.personal_window_days === 1095 ? "3 years" : "1 year") : translate("unavailable")}
              {data.benchmark_window_days ? ` · ${translate("Benchmark:")} ${translate(data.benchmark_window_days === 1095 ? "3 years" : "1 year")}` : ""}
              {" · "}{translate(data.basis === "nominal_toman" ? "Nominal Toman (inflation data unavailable)" : "Real Toman")}
            </p>
            {data.fallback_reason && <p className="text-sm text-[var(--c-warn-text)]" data-testid="guidance-fallback">{data.fallback_reason}</p>}
            <Allocation title="For my portfolio" weights={data.personal?.target_weights} labelFor={labelFor} testId="guidance-personal" />
            <Allocation title="Market benchmark" weights={data.benchmark?.target_weights} labelFor={labelFor} testId="guidance-benchmark" />
          </div>
        )}
      </Async>
    </div>
  );
}

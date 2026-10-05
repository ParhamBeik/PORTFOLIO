import { useEffect, useMemo, useState } from "react";
import { translate } from "../i18n.js";
import { listAssets, priceHistory } from "../api.js";
import { MultiLineTrend } from "../components/charts.jsx";
import { Async, Card, Empty, PageHeader, Select, Tabs } from "../components/ui.jsx";
import { catalogLabel, date, num, toman } from "../format.js";
import { useApi } from "../useApi.js";

// Days of calendar history, which is what the server now windows on.
const WINDOWS = [
  { value: "90", label: "90D" },
  { value: "365", label: "1Y" },
  { value: "1825", label: "5Y" },
];

function nativePrice(value, unit) {
  if (unit === "Rial") return `${num(value, 0)} Rial`;
  return toman(value);
}

// Prices are global, not per-account, so this page deliberately does not read
// the active portfolio: the same series answers for every user.
export default function AssetHistory() {
  const assets = useApi(listAssets, []);
  const [assetKey, setAssetKey] = useState("");
  const [window, setWindow] = useState("365");

  const choices = useMemo(
    () => (assets.data || []).filter((asset) => !asset.is_house),
    [assets.data],
  );

  useEffect(() => {
    if (!assetKey && choices.length) setAssetKey(choices[0].key);
    if (assetKey && !choices.some((asset) => asset.key === assetKey)) setAssetKey("");
  }, [assetKey, choices]);

  const selected = choices.find((asset) => asset.key === assetKey);
  const history = useApi(
    () => priceHistory(assetKey, Number(window)),
    [assetKey, window],
    { enabled: !!assetKey },
  );

  return (
    <div>
      <PageHeader
        title="Price history"
        subtitle="See one asset's actual daily closes before comparing it with your portfolio."
      />
      <Card
        title={selected ? catalogLabel(selected) : "Choose an asset"}
        testId="asset-history-card"
        actions={
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs font-medium tracking-wide text-muted uppercase">
              {translate("Asset")}
              <Select
                label="Asset"
                value={assetKey}
                onChange={(e) => setAssetKey(e.target.value)}
                data-testid="asset-history-asset"
                className="min-w-52"
              >
                <option value="">{translate("Choose…")}</option>
                {choices.map((asset) => (
                  <option key={asset.key} value={asset.key}>
                    <bdi>{catalogLabel(asset)}</bdi>
                  </option>
                ))}
              </Select>
            </label>
            <Tabs
              options={WINDOWS}
              value={window}
              onChange={setWindow}
              label="History window"
              testId="asset-history-window"
            />
          </div>
        }
      >
        <Async
          {...history}
          testId="asset-history-result"
          empty="No price history is available for this asset yet."
          minHeight="320px"
        >
          {(data) => {
            if (!data.points?.length) {
              return <Empty testId="asset-history-empty">No price history is available yet.</Empty>;
            }
            const rows = data.points.map((point) => ({ x: point.date, price: point.price }));
            const unit = data.asset?.unit || "Toman";
            return (
              <div className="space-y-2">
                <MultiLineTrend
                  series={[{ key: "price", name: `${data.asset.symbol} (${unit})` }]}
                  data={rows}
                  longTicks={rows.length > 180}
                  formatValue={(value) => nativePrice(value, unit)}
                  formatAxis={(value) => nativePrice(value, unit)}
                  label={`${data.asset.name} price history`}
                />
                <p className="text-xs text-muted" data-testid="asset-history-note">
                  Source: {data.source}. {date(data.earliest_date)} → {date(data.latest_date)}.
                  {data.asset.asset_class === "Crypto" &&
                    " Crypto history begins when this deployment started collecting it; earlier prices are not inferred."}
                </p>
              </div>
            );
          }}
        </Async>
      </Card>
    </div>
  );
}

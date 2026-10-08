import { accountDataQuality, valuation } from "../api.js";
import { translate, useLang } from "../i18n.js";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { Async, Badge, Card, Table } from "../components/ui.jsx";
import { ago, dateTime, holdingLabel, humanize } from "../format.js";
import { useApi } from "../useApi.js";

const LIVE_TONE = { live: "good", manual: "neutral", stale: "warn", quota: "warn", fallback: "warn", unavailable: "critical" };
const REPAIR_TONE = {
  complete: "good", refresh_due: "good", awaiting_data: "neutral", not_tried: "neutral",
  failed: "critical", suspended: "warn", unfetchable: "critical", not_scheduled: "warn",
};
const REPAIR_LABEL = {
  complete: "Complete", refresh_due: "Complete · refresh due", awaiting_data: "Waiting for first data",
  not_tried: "Queued", failed: "Retrying after failures", suspended: "Paused by operator",
  unfetchable: "Provider has no data", not_scheduled: "No backfill job",
};

/**
 * Where every number comes from, in one place. Home stays clean; this page
 * holds the source, the time, the status and what is being done about a gap,
 * so a figure that looks wrong can be checked without guessing.
 */
export default function DataHealth() {
  const { activeId, accounts } = usePortfolio();
  const live = useApi(() => valuation(activeId, "nominal_toman"), [activeId]);
  const scoped = activeId ? accounts.filter((a) => a.id === activeId) : accounts;

  return (
    <div className="space-y-6" data-testid="data-health">
      <Card
        title="Current prices"
        subtitle="Where each holding's price comes from right now and how old it is."
        testId="data-health-live"
      >
        <Async {...live} testId="data-health-live-body">
          {(data) => (
            <Table
              testId="data-health-live-table"
              mobileCards
              rowKey={(r) => `${r.account_id ?? ""}-${r.key}`}
              rows={data.items || []}
              empty="No holdings to check."
              columns={[
                { key: "label", header: "Holding", render: (r) => <bdi>{holdingLabel(r)}</bdi> },
                { key: "source", header: "Source", render: (r) => r.source || "—" },
                {
                  key: "priced_at",
                  header: "Priced",
                  render: (r) => (r.priced_at ? <span title={dateTime(r.priced_at)}>{ago(r.age_seconds)}</span> : "—"),
                },
                {
                  key: "quality_status",
                  header: "Status",
                  align: "right",
                  render: (r) => (
                    <Badge variant={LIVE_TONE[r.quality_status] || "neutral"}>
                      {humanize(r.quality_status || "unknown")}
                      {r.price_unit_status === "unverified" ? " · unit unverified" : ""}
                    </Badge>
                  ),
                },
              ]}
            />
          )}
        </Async>
      </Card>
      {scoped.map((account) => (
        <HistoryHealth key={account.id} account={account} />
      ))}
    </div>
  );
}

function HistoryHealth({ account }) {
  const lang = useLang();
  const state = useApi(() => accountDataQuality(account.id), [account.id]);
  return (
    <Card
      title={`${translate("Price history", lang)} · ${account.name}`}
      subtitle="Whether the history behind returns and charts is complete, and what the backfill is doing about any gap."
      testId={`data-health-history-${account.id}`}
    >
      <Async {...state} testId={`data-health-history-body-${account.id}`}>
        {(data) => (
          <Table
            mobileCards
            rowKey={(r) => r.asset_key}
            rows={data.assets || []}
            empty="No priced holdings in this portfolio."
            columns={[
              { key: "symbol", header: "Holding", render: (r) => <bdi>{r.symbol || r.asset_key}</bdi> },
              {
                key: "gate",
                header: "History",
                render: (r) =>
                  r.passes_gate == null ? translate("Manual value", lang) : r.passes_gate ? translate("Complete enough", lang) : (r.reason_codes || []).map(humanize).join(", ") || translate("Gaps", lang),
              },
              {
                key: "repair",
                header: "Backfill",
                align: "right",
                render: (r) =>
                  r.repair_state ? (
                    <span className="inline-flex flex-col items-end gap-0.5">
                      <Badge variant={REPAIR_TONE[r.repair_state] || "neutral"}>{REPAIR_LABEL[r.repair_state] || humanize(r.repair_state)}</Badge>
                      {r.next_repair_at && <span className="text-xs text-muted">next try {dateTime(r.next_repair_at)}</span>}
                    </span>
                  ) : "—",
              },
            ]}
          />
        )}
      </Async>
    </Card>
  );
}

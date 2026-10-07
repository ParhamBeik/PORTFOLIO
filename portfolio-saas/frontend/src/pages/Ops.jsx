import { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate } from "react-router-dom";
import {
  adminArchiveRetry,
  adminArchiveStates,
  adminAssets,
  adminAssetEvidence,
  adminAssetRecomputeIntegrity,
  adminAssetRefresh,
  adminAssetRetry,
  adminOverview,
  adminPasswordResetLink,
  adminSetUserActive,
  adminUsers,
  adminWorkflows,
} from "../api.js";
import { CountTrend, Donut, StackedStatusBar } from "../components/charts.jsx";
import {
  Badge,
  Button,
  Card,
  Delta,
  Input,
  PageHeader,
  Pager,
  StatTile,
  Table,
  Tabs,
} from "../components/ui.jsx";
import { dateTime, humanize, num } from "../format.js";

const POLL_MS = 10000;

// Every status-ish string this console renders lands on one of four badge
// tones. Five near-identical mappers disagreed about the overlap (is "partial"
// warn or neutral?); one table cannot. Unknown values stay neutral rather than
// guessing -- including "complete", which has always read as neutral here.
const TONE = {
  healthy: "good", fresh: "good", success: "good", pass: "good",
  // `refresh_due` is a HEALTHY state: the archive deliberately re-arms a
  // completed job so it picks up the sessions printed since, and one parked
  // behind the daily quota ceiling holds every row it expects. Grading it as a
  // problem is what made 52.8% of the warehouse look broken.
  refresh_due: "good",
  degraded: "warn", stale: "warn", partial: "warn", retry: "warn",
  skipped: "warn", not_assessed: "warn", awaiting_data: "warn",
  critical: "critical", failed: "critical", fail: "critical",
  missing: "critical", blocked_network: "critical", blocked_storage: "critical",
  // Not "critical": a blacklisted or peer-suspended symbol is a settled
  // decision, not an incident to page on. It is still not coverage, so it must
  // not read as good either.
  unfetchable: "warn", suspended: "warn",
};

const tone = (value) => TONE[value] || "neutral";

// A measurement the collector never took is not the number zero. `|| 0` here is
// what drew a spike to the floor and back on a table that only ever grows.
const nullable = (value) => (value == null ? null : Number(value));

function gb(bytes) {
  if (bytes == null) return "—";
  return `${(Number(bytes) / 1024 ** 3).toFixed(2)} GB`;
}

// Mirrors `coverage_report.classify_archive_state`. Kept in step with it: a
// re-armed job that owes no rows is healthy, not partial, and a symbol the
// archive has given up on is neither complete nor merely failing.
function archiveJobVariant(row) {
  if (row.blacklisted) return "unfetchable";
  if (row.suspended_at) return "suspended";
  if (row.verified_complete) return "complete";
  if ((row.consecutive_failures || 0) > 0) return "failed";
  if (!row.last_attempt_at) return "not_tried";
  if (!row.last_success_at) return "awaiting_data";
  if ((row.missing_rows || 0) === 0) return "refresh_due";
  return "partial";
}

function historySeries(history, key) {
  return (history || []).map((row) => ({
    x: row.captured_at,
    y: nullable(row.counts?.[key]),
  }));
}

const LIVE_TABLE_KEYS = new Set(["prices", "snapshots"]);
const WH_TABLE_KEYS = new Set([
  "market_instruments",
  "stock_history_rows",
  "gold_currency_rows",
  "candles",
  "stock_transaction_ticks",
  "announcements",
  "shareholders",
]);

function SortHeader({ label, field, ordering, onSort }) {
  const active = ordering === field || ordering === `-${field}`;
  const desc = ordering === `-${field}`;
  return (
    <button
      type="button"
      className={`inline-flex items-center gap-1 text-left text-xs font-medium tracking-wide uppercase ${active ? "text-text" : "text-muted hover:text-text"}`}
      onClick={() => onSort(field)}
    >
      {label}
      {active && <span aria-hidden>{desc ? "↓" : "↑"}</span>}
    </button>
  );
}

function AssetDetailPanel({ lookup, evidence, error, busy, onAct }) {
  if (!lookup) {
    return (
      <Card title="Asset detail" subtitle="Select a row in the catalog to inspect claims, archive jobs, and repair actions.">
        <p className="text-sm text-muted">No asset selected.</p>
      </Card>
    );
  }
  if (error && !evidence) {
    return (
      <Card title="Asset detail">
        <p role="alert" className="text-sm text-[var(--c-warn-text)]">{error}</p>
      </Card>
    );
  }
  if (!evidence) {
    return (
      <Card title="Asset detail">
        <p className="text-sm text-muted">Loading {lookup}…</p>
      </Card>
    );
  }

  const claims = evidence.claims || [];
  const passedClaims = claims.filter((c) => c.passed).length;

  return (
    <div className="space-y-4" data-testid="ops-asset-evidence">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Claims passing" value={`${passedClaims}/${claims.length}`} sub="Live · archive · 179d gate" />
        <StatTile
          label="Live price"
          value={evidence.displayed_value?.quality_status === "live" ? "Fresh" : humanize(evidence.displayed_value?.quality_status || "missing")}
          sub={evidence.displayed_value?.age_seconds != null ? `${num(evidence.displayed_value.age_seconds)}s old` : "No row"}
          valueTone={evidence.displayed_value?.quality_status === "live" ? "good" : "warn"}
        />
        <StatTile label="Archive jobs" value={num((evidence.archive_states || []).length)} sub={`${num((evidence.archive_states || []).filter((s) => s.verified_complete).length)} verified`} />
        <StatTile label="179d gate" value={evidence.integrity?.passes_gate ? "Pass" : "Fail"} sub={evidence.integrity?.reason || "ok"} valueTone={evidence.integrity?.passes_gate ? "good" : "critical"} />
      </div>

      <Card title={`${evidence.identity?.name || evidence.identity?.key || lookup}`} subtitle={`key ${evidence.identity?.key || "—"} · TSE ${evidence.identity?.tse_symbol || "—"} · BRS ${evidence.identity?.brs_symbol || "—"}`}>
        <div className="flex flex-wrap gap-2">
          {claims.map((c) => (
            <Badge key={c.id} variant={c.passed ? "good" : "warn"} title={c.definition} testId={`ops-claim-${c.id}`}>
              {c.label}: {c.passed ? "yes" : "no"}
            </Badge>
          ))}
        </div>
        <p className="mt-2 text-sm text-muted">
          Live {evidence.displayed_value?.price || "—"} {evidence.displayed_value?.unit || ""} · {evidence.displayed_value?.source || "—"}
        </p>
        {evidence.suggested_cli && (
          <p className="mt-2 text-xs text-muted" data-testid="ops-asset-cli">Repair CLI: <code>{evidence.suggested_cli}</code></p>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          <Button disabled={!!busy} onClick={() => onAct(adminAssetRetry, "retry")} data-testid="ops-asset-retry">Retry failed</Button>
          <Button disabled={!!busy} onClick={() => onAct(adminAssetRefresh, "refresh")} data-testid="ops-asset-refresh">Refresh series</Button>
          <Button disabled={!!busy} onClick={() => onAct(adminAssetRecomputeIntegrity, "recompute")} data-testid="ops-asset-recompute">Recompute gate</Button>
        </div>
      </Card>

      <Card title="Archive jobs for this symbol" subtitle="Per-endpoint fetch state from the warehouse.">
        <Table
          testId="ops-asset-archive"
          rows={evidence.archive_states || []}
          rowKey={(r) => r.id}
          empty="No archive states linked to this asset."
          columns={[
            { key: "endpoint", header: "Endpoint" },
            {
              key: "state",
              header: "State",
              render: (r) => (
                <Badge variant={tone(archiveJobVariant(r))}>{humanize(archiveJobVariant(r))}</Badge>
              ),
            },
            { key: "stored_rows", header: "Stored", align: "right", render: (r) => num(r.stored_rows) },
            { key: "missing_rows", header: "Missing", align: "right", render: (r) => num(r.missing_rows) },
            { key: "failures", header: "Failures", align: "right", render: (r) => num(r.consecutive_failures) },
            { key: "last_error", header: "Last error", render: (r) => r.last_error || "—" },
          ]}
        />
      </Card>

      <Card title="179-day analytics gate" testId="ops-asset-gate">
        <p className="text-sm">Coverage {pctRatio(evidence.integrity?.coverage_ratio)} · max gap {evidence.integrity?.max_gap_days ?? "—"} · missing {evidence.integrity?.missing_count ?? "—"}</p>
        <p className="text-sm text-muted">{evidence.integrity?.reason || "Passes gate"}</p>
      </Card>

      <Card title="Recent workflows" subtitle="Last 25 runs touching this symbol.">
        <Table
          testId="ops-asset-workflows"
          rows={evidence.workflows || []}
          rowKey={(r) => r.id}
          empty="No workflow history for this symbol."
          columns={[
            { key: "created_at", header: "When", render: (r) => dateTime(r.created_at) },
            { key: "workflow", header: "Workflow" },
            { key: "outcome", header: "Outcome", render: (r) => <Badge variant={tone(r.outcome)}>{humanize(r.outcome)}</Badge> },
            { key: "endpoint", header: "Endpoint" },
            { key: "error_code", header: "Error", render: (r) => r.error_code || "—" },
          ]}
        />
      </Card>
    </div>
  );
}

function AssetInspector() {
  const [assetClass, setAssetClass] = useState("all");
  const [search, setSearch] = useState("");
  const [searchDebounced, setSearchDebounced] = useState("");
  const [liveFilter, setLiveFilter] = useState("all");
  const [integrityFilter, setIntegrityFilter] = useState("all");
  const [heldOnly, setHeldOnly] = useState(false);
  const [ordering, setOrdering] = useState("name");
  const [page, setPage] = useState(1);
  const [catalog, setCatalog] = useState({ results: [], count: 0, asset_classes: [] });
  const [catalogError, setCatalogError] = useState("");
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [selectedKey, setSelectedKey] = useState("");
  const [evidence, setEvidence] = useState(null);
  const [evidenceError, setEvidenceError] = useState("");
  const [busy, setBusy] = useState("");

  useEffect(() => {
    const id = setTimeout(() => setSearchDebounced(search.trim()), 250);
    return () => clearTimeout(id);
  }, [search]);

  useEffect(() => {
    setPage(1);
  }, [assetClass, searchDebounced, liveFilter, integrityFilter, heldOnly, ordering]);

  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true);
    setCatalogError("");
    try {
      const data = await adminAssets({
        asset_class: assetClass === "all" ? undefined : assetClass,
        search: searchDebounced || undefined,
        live_status: liveFilter === "all" ? undefined : liveFilter,
        integrity: integrityFilter === "all" ? undefined : integrityFilter,
        held_only: heldOnly ? "true" : undefined,
        ordering,
        page,
        page_size: 50,
      });
      setCatalog(data);
    } catch (err) {
      setCatalogError(err?.message || "Failed to load asset catalog");
    } finally {
      setCatalogLoading(false);
    }
  }, [assetClass, searchDebounced, liveFilter, integrityFilter, heldOnly, ordering, page]);

  useEffect(() => {
    loadCatalog();
  }, [loadCatalog]);

  const loadEvidence = useCallback(async (key) => {
    if (!key) {
      setEvidence(null);
      setEvidenceError("");
      return;
    }
    setEvidenceError("");
    try {
      setEvidence(await adminAssetEvidence(key));
    } catch (err) {
      setEvidence(null);
      setEvidenceError(err?.message || "Failed to load asset evidence");
    }
  }, []);

  useEffect(() => {
    loadEvidence(selectedKey);
  }, [selectedKey, loadEvidence]);

  const selectRow = (row) => {
    setSelectedKey(row.key);
  };

  const toggleSort = (field) => {
    setOrdering((cur) => (cur === field ? `-${field}` : field));
  };

  const act = async (fn, label) => {
    if (!selectedKey) return;
    setBusy(label);
    setEvidenceError("");
    try {
      await fn(selectedKey);
      await loadEvidence(selectedKey);
      await loadCatalog();
    } catch (err) {
      setEvidenceError(err?.message || `${label} failed`);
    } finally {
      setBusy("");
    }
  };

  const classOptions = useMemo(() => {
    const fromApi = catalog.asset_classes || [];
    if (fromApi.length) return fromApi.map((row) => ({ value: row.value, label: `${row.label} (${num(row.count)})` }));
    return [{ value: "all", label: "All classes" }];
  }, [catalog.asset_classes]);

  const pages = Math.max(1, Math.ceil((catalog.count || 0) / 50));
  const attentionCount = (catalog.results || []).filter((r) => (
    r.live_status === "stale" || r.live_status === "missing" || r.integrity_status === "fail" || r.archive_status === "failed"
  )).length;

  return (
    <div className="space-y-4" data-testid="ops-asset">
      <Card
        title="Asset catalog"
        subtitle="Browse every active asset by class — filter, sort, then select a row to inspect or repair."
      >
        <div className="space-y-3">
          <Tabs
            label="Asset class"
            testId="ops-asset-class"
            value={assetClass}
            onChange={setAssetClass}
            options={classOptions}
          />

          <div className="flex flex-wrap items-end gap-3">
            <label className="flex min-w-[14rem] flex-1 flex-col gap-1">
              <span className="text-xs font-medium uppercase tracking-wide text-muted">Search name, key, or symbol</span>
              <input
                aria-label="Search assets"
                className="app-toolbar-select w-full rounded-md border border-border bg-panel px-2.5 py-2 text-sm"
                placeholder="کاما, kama, emami…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                data-testid="ops-asset-search"
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-muted">
              <input type="checkbox" checked={heldOnly} onChange={(e) => setHeldOnly(e.target.checked)} data-testid="ops-asset-held-only" />
              Held in portfolio only
            </label>
          </div>

          <div className="flex flex-wrap gap-2">
            <Tabs
              label="Live price filter"
              testId="ops-asset-live-filter"
              value={liveFilter}
              onChange={setLiveFilter}
              options={[
                { value: "all", label: "All live states" },
                { value: "fresh", label: "Fresh" },
                { value: "stale", label: "Stale" },
                { value: "missing", label: "Missing" },
              ]}
            />
            <Tabs
              label="Integrity filter"
              testId="ops-asset-integrity-filter"
              value={integrityFilter}
              onChange={setIntegrityFilter}
              options={[
                { value: "all", label: "All gates" },
                { value: "pass", label: "Passing" },
                { value: "fail", label: "Failing" },
                { value: "not_assessed", label: "Not assessed" },
              ]}
            />
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile label="Matching assets" value={num(catalog.count)} sub={catalogLoading ? "Loading…" : `${num(catalog.results?.length)} on this page`} />
            <StatTile label="Needs attention" value={num(attentionCount)} sub="Stale, missing, failed gate, or archive" valueTone={attentionCount ? "warn" : "good"} />
            <StatTile label="Selected" value={selectedKey || "—"} sub="Click a row below" />
            <StatTile label="Sort" value={ordering.replace("-", "↓ ")} sub="Click column headers" />
          </div>

          {catalogError && <p role="alert" className="text-sm text-[var(--c-warn-text)]">{catalogError}</p>}

          <div className="overflow-x-auto rounded-lg border border-border" data-testid="ops-asset-catalog">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-panel-2 text-left">
                  {[
                    ["display_name", "Name"],
                    ["key", "Key"],
                    ["asset_class", "Class"],
                    [null, "TSE / BRS"],
                    ["live_status", "Live"],
                    ["age_seconds", "Age"],
                    ["integrity_status", "179d gate"],
                    [null, "Archive"],
                    [null, "Held"],
                  ].map(([field, label]) => (
                    <th key={label} scope="col" className="px-3 py-2">
                      {field ? (
                        <SortHeader label={label} field={field} ordering={ordering} onSort={toggleSort} />
                      ) : (
                        <span className="text-xs font-medium tracking-wide text-muted uppercase">{label}</span>
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(catalog.results || []).length ? (
                  catalog.results.map((row) => {
                    const selected = row.key === selectedKey;
                    return (
                      <tr
                        key={row.key}
                        data-testid="ops-asset-row"
                        className={`cursor-pointer border-b border-border/60 last:border-0 hover:bg-panel-2 ${selected ? "bg-accent/10" : ""}`}
                        onClick={() => selectRow(row)}
                        aria-selected={selected}
                      >
                        <td className="px-3 py-2 font-medium">{row.display_name || row.name}</td>
                        <td className="px-3 py-2 font-mono text-xs text-muted">{row.key}</td>
                        <td className="px-3 py-2">{row.asset_class}</td>
                        <td className="px-3 py-2 text-xs text-muted">
                          {row.tse_symbol || "—"}{row.brs_symbol ? ` · ${row.brs_symbol}` : ""}
                        </td>
                        <td className="px-3 py-2">
                          <Badge variant={tone(row.live_status)}>{humanize(row.live_status)}</Badge>
                        </td>
                        <td className="px-3 py-2 tabular text-right text-muted">
                          {row.age_seconds != null ? `${num(row.age_seconds)}s` : "—"}
                        </td>
                        <td className="px-3 py-2">
                          <Badge variant={tone(row.integrity_status)}>{humanize(row.integrity_status)}</Badge>
                        </td>
                        <td className="px-3 py-2">
                          {row.archive_status ? (
                            <Badge variant={tone(row.archive_status)}>{humanize(row.archive_status)}</Badge>
                          ) : (
                            <span className="text-muted">—</span>
                          )}
                        </td>
                        <td className="px-3 py-2">{row.held ? "Yes" : "—"}</td>
                      </tr>
                    );
                  })
                ) : (
                  <tr>
                    <td colSpan={9} className="px-3 py-8 text-center text-sm text-muted">
                      {catalogLoading ? "Loading catalog…" : "No assets match these filters."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <Pager page={page} count={catalog.count} pageSize={50} onPage={setPage} />
          {pages > 1 && <p className="text-xs text-muted">Page {page} of {pages}</p>}
        </div>
      </Card>

      <AssetDetailPanel
        lookup={selectedKey}
        evidence={evidence}
        error={evidenceError}
        busy={busy}
        onAct={act}
      />
    </div>
  );
}




const WAREHOUSE_SERIES = [
  { key: "complete", name: "Complete" },
  { key: "refresh_due", name: "Refresh due" },
  { key: "partial", name: "Rows missing" },
  { key: "failed", name: "Failing" },
  { key: "awaiting_data", name: "No payload yet" },
  { key: "not_tried", name: "Not tried" },
  { key: "unfetchable", name: "Unfetchable" },
  { key: "suspended", name: "Suspended" },
];

function totalsToDonut(totals, labels) {
  return Object.entries(totals || {})
    .filter(([, v]) => Number(v) > 0)
    .map(([key, value]) => ({ name: labels?.[key] || key, value: Number(value), key }));
}

function LiveCoveragePanel({ coverage }) {
  const [scope, setScope] = useState("held");
  const live = coverage?.live;
  const block = scope === "held" ? live?.held : live?.catalog;
  if (!coverage?.live) return null;
  if (!block) return null;
  const labels = block.status_labels || {};
  const donut = totalsToDonut(block.totals, labels);
  const integrity = block.integrity || {};
  const rows = block.assets || [];

  return (
    <div className="space-y-4" data-testid={scope === "held" ? "ops-live-held" : "ops-live-catalog"}>
      <Tabs
        label="Live pipeline scope"
        testId="ops-live-scope"
        value={scope}
        onChange={setScope}
        options={[
          { value: "held", label: "Held assets" },
          { value: "catalog", label: "Full catalog" },
        ]}
      />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Fetchable assets" value={num(block.fetchable_assets)} sub={`${block.total_assets} in ${block.scope}`} />
        <StatTile label="Fresh live prices" value={`${block.fresh_pct ?? 0}%`} sub={`${num(block.fresh_assets)} / ${num(block.fetchable_assets)}`} />
        <StatTile label="Stale / missing" value={num((block.totals?.stale || 0) + (block.totals?.missing || 0))} sub="Needs attention" />
        <StatTile label="179d gate" value={`${integrity.passing || 0} pass`} sub={`${integrity.failing || 0} fail · ${integrity.not_assessed || 0} not assessed`} />
      </div>
      {donut.length > 0 && (
        <Card title="Live price status" subtitle="Portfolio valuation feed — not warehouse archive fill." testId="ops-live-donut">
          <Donut data={donut} valueFormat={(v) => String(v)} testId="ops-live-donut-chart" />
        </Card>
      )}
      {(coverage?.warehouse?.live_sourced || []).length > 0 && (
        <Card
          title="Live-sourced market data"
          subtitle="Fetched on the live price loop (BRS + TSETMC). History accumulates from repeated snapshots — not per-symbol archive backfill."
          testId="ops-live-sourced"
        >
          <ul className="space-y-2 text-sm">
            {(coverage.warehouse.live_sourced || []).map((row) => (
              <li key={row.endpoint} className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between">
                <span className="font-medium">{row.label}</span>
                <span className="text-xs text-muted sm:max-w-[60%] sm:text-right">{row.note}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card title={scope === "held" ? "Held assets" : "Full asset catalog"} subtitle="Validated from latest Price row age and asset configuration." testId="ops-live-table">
        <Table
          rows={rows}
          rowKey={(r) => r.key}
          columns={[
            { key: "name", header: "Asset", render: (r) => r.name || r.key },
            { key: "status", header: "Status", render: (r) => <Badge variant={tone(r.status)}>{humanize(r.status)}</Badge> },
            { key: "class", header: "Class", render: (r) => r.asset_class },
            { key: "age", header: "Age", align: "right", render: (r) => (r.age_seconds != null ? `${num(r.age_seconds)}s` : "—") },
            { key: "source", header: "Source", render: (r) => r.source || "—" },
          ]}
        />
      </Card>
    </div>
  );
}

function WarehouseCoveragePanel({ warehouse }) {
  if (!warehouse) return null;
  const labels = warehouse.status_labels || {};
  const donut = totalsToDonut(warehouse.counts, labels);
  const barData = (warehouse.by_endpoint || [])
    .filter((row) => row.total > 0)
    .map((row) => ({
      name: row.label.replace(/^Stock /, "").slice(0, 28),
      complete: row.counts?.complete || 0,
      refresh_due: row.counts?.refresh_due || 0,
      partial: row.counts?.partial || 0,
      failed: row.counts?.failed || 0,
      awaiting_data: row.counts?.awaiting_data || 0,
      not_tried: row.counts?.not_tried || 0,
      unfetchable: row.counts?.unfetchable || 0,
      suspended: row.counts?.suspended || 0,
    }));
  const backlog = warehouse.refresh_backlog || {};
  const census = warehouse.symbol_census || {};

  return (
    <div className="space-y-4" data-testid="ops-warehouse">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile
          label="Holding every row"
          value={`${warehouse.healthy_pct ?? 0}%`}
          sub={`${num(warehouse.counts?.complete)} verified · ${num(warehouse.counts?.refresh_due)} refresh due`}
          valueTone={(warehouse.healthy_pct ?? 0) >= 90 ? "good" : "warn"}
        />
        <StatTile
          label="Rows missing"
          value={num(warehouse.counts?.partial)}
          sub={`${warehouse.partial_pct || 0}% of jobs · ${num(warehouse.missing_rows)} rows`}
          valueTone={(warehouse.counts?.partial || 0) > 0 ? "warn" : "good"}
        />
        <StatTile
          label="Failing"
          value={num(warehouse.counts?.failed)}
          sub={`${warehouse.failed_pct || 0}% consecutive failures`}
          valueTone={(warehouse.counts?.failed || 0) > 0 ? "critical" : "good"}
        />
        <StatTile
          label="No payload yet"
          value={num((warehouse.counts?.awaiting_data || 0) + (warehouse.counts?.not_tried || 0))}
          sub={`${num(warehouse.counts?.awaiting_data)} attempted · ${num(warehouse.counts?.not_tried)} never tried`}
        />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5" data-testid="ops-warehouse-census">
        <StatTile
          label="Symbols fetched"
          value={num(census.fetched)}
          sub={`${census.fetched_pct ?? 0}% of ${num(census.symbols_total)} symbols`}
          valueTone={(census.fetched_pct ?? 0) >= 90 ? "good" : "warn"}
          testId="ops-warehouse-census-fetched"
        />
        <StatTile
          label="Never fetched"
          value={num(census.never_fetched)}
          sub="No archive job has ever been attempted"
          valueTone={(census.never_fetched || 0) > 0 ? "warn" : "good"}
          testId="ops-warehouse-census-never"
        />
        <StatTile
          label="Attempted, nothing landed"
          value={num(census.attempted_never_landed)}
          sub="Tried at least once, still empty"
          valueTone={(census.attempted_never_landed || 0) > 0 ? "warn" : "good"}
          testId="ops-warehouse-census-empty"
        />
        <StatTile
          label="Unfetchable"
          value={num(census.unfetchable)}
          sub="Every endpoint given up on"
          valueTone={(census.unfetchable || 0) > 0 ? "warn" : "good"}
          testId="ops-warehouse-census-unfetchable"
        />
        <StatTile
          label="Partly unfetchable"
          value={num(census.partially_unfetchable)}
          sub="Some endpoints given up, others still live"
          testId="ops-warehouse-census-partial"
        />
      </div>
      {/* The refresh queue's own health. A completed job is deliberately
          re-armed so it picks up newly printed sessions, so the question that
          matters is not "is anything unverified" but "how far behind is the
          furthest-behind symbol". */}
      <Card
        title="Refresh backlog"
        subtitle="Days since each job last landed a payload — the order the re-fetch queue drains in."
        testId="ops-warehouse-backlog"
      >
        <p className="text-sm">
          {num(backlog.tracked)} jobs tracked · median {num(backlog.median_days)}d behind · oldest {num(backlog.max_days)}d
        </p>
        {(backlog.oldest || []).length > 0 && (
          <ul className="mt-2 space-y-1 text-xs" data-testid="ops-warehouse-backlog-list">
            {backlog.oldest.map((row) => (
              <li key={`${row.symbol}:${row.endpoint}`} className="flex items-center justify-between gap-3">
                <span className="truncate">{row.symbol} · <span className="text-muted">{humanize(row.endpoint)}</span></span>
                <Badge variant={tone(row.status)}>{num(row.stale_days)}d</Badge>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Row fill (honest)" subtitle="Per-symbol historical backfill. Commodities, crypto, index, and options live on the Live pipeline tab." testId="ops-warehouse-rows">
        <p className="text-sm">
          {num(warehouse.stored_rows)} stored / {num(warehouse.expected_rows)} expected ({warehouse.row_fill_pct ?? 0}%)
          · {num(warehouse.missing_rows)} missing · {num(warehouse.known_gap_rows)} known gaps
        </p>
      </Card>
      {donut.length > 0 && (
        <Card title="Job lifecycle" subtitle="Per symbol×endpoint ArchiveFetchState — validated from attempt timestamps and failure counters." testId="ops-warehouse-donut">
          <Donut data={donut} valueFormat={(v) => String(v)} />
        </Card>
      )}
      {barData.length > 0 && (
        <Card title="By endpoint" subtitle="Stacked job counts — not a smoothed progress percentage." testId="ops-completeness">
          <StackedStatusBar data={barData} series={WAREHOUSE_SERIES} label="Archive jobs by endpoint and status" testId="ops-warehouse-stack" />
        </Card>
      )}
      <Table
        testId="ops-warehouse-endpoints"
        rows={warehouse.by_endpoint || []}
        rowKey={(r) => r.endpoint}
        columns={[
          { key: "label", header: "Endpoint" },
          { key: "total", header: "Jobs", align: "right", render: (r) => num(r.total) },
          { key: "complete", header: "Complete", align: "right", render: (r) => num(r.counts?.complete) },
          { key: "refresh_due", header: "Refresh due", align: "right", render: (r) => num(r.counts?.refresh_due) },
          { key: "partial", header: "Rows missing", align: "right", render: (r) => num(r.counts?.partial) },
          { key: "failed", header: "Failing", align: "right", render: (r) => num(r.counts?.failed) },
          { key: "awaiting_data", header: "No payload", align: "right", render: (r) => num((r.counts?.awaiting_data || 0) + (r.counts?.not_tried || 0)) },
          { key: "unfetchable", header: "Unfetchable", align: "right", render: (r) => num(r.counts?.unfetchable) },
          { key: "suspended", header: "Suspended", align: "right", render: (r) => num(r.counts?.suspended) },
          { key: "row_fill", header: "Row fill", align: "right", render: (r) => (r.row_fill_pct != null ? `${r.row_fill_pct}%` : "—") },
        ]}
      />
    </div>
  );
}

function TablesPanel({ overview }) {
  const tables = overview?.coverage?.tables;
  const rows = overview?.database_rows || [];
  const inventory = overview?.admin_model_inventory || [];
  if (!tables) return null;

  const liveRows = rows.filter((r) => LIVE_TABLE_KEYS.has(r.key));
  const whRows = rows.filter((r) => WH_TABLE_KEYS.has(r.key));
  const delta24 = rows.reduce((s, r) => s + (Number(r.delta_24h) || 0), 0);
  const splitDonut = [
    { name: "Live pipeline", value: tables.live_row_total || 0 },
    { name: "Warehouse", value: tables.warehouse_row_total || 0 },
  ].filter((d) => d.value > 0);
  const domainDonut = Object.entries(inventory.reduce((counts, row) => {
    counts[row.app] = (counts[row.app] || 0) + 1;
    return counts;
  }, {})).map(([name, value]) => ({ name, value }));

  const tableCol = [
    { key: "label", header: "Table" },
    { key: "count", header: "Rows", align: "right", render: (r) => num(r.count) },
    { key: "bytes", header: "Size", align: "right", render: (r) => gb(r.bytes) },
    {
      key: "delta_24h",
      header: "24h Δ",
      align: "right",
      render: (r) => (r.delta_24h == null ? "—" : <Delta value={r.delta_24h} format={(v) => num(v)} />),
    },
    {
      key: "delta_7d",
      header: "7d Δ",
      align: "right",
      // A table cannot shed more rows than it holds through ordinary churn, so a
      // delta that large is a deletion -- a purge or a migration -- not a
      // collapsing ingest. Candles showed -5,786,949 against 1,734,366 stored
      // and read as a catastrophe with nothing to say otherwise.
      render: (r) =>
        r.delta_7d == null ? (
          "—"
        ) : (
          <span
            title={
              Number(r.delta_7d) < -Number(r.count || 0)
                ? "Larger than the table itself — rows were deleted in bulk (a purge or migration), not lost by the ingest."
                : undefined
            }
          >
            <Delta value={r.delta_7d} format={(v) => num(v)} />
            {Number(r.delta_7d) < -Number(r.count || 0) && (
              <span className="ml-1 text-muted" data-testid="ops-tables-bulk-delete">
                (bulk delete)
              </span>
            )}
          </span>
        ),
    },
    { key: "latest", header: "Latest data", render: (r) => dateTime(r.latest) },
  ];

  const history = overview.database_history || [];

  return (
    <div className="space-y-4" data-testid="ops-tables">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Total tracked rows" value={num((tables.live_row_total || 0) + (tables.warehouse_row_total || 0))} sub={`${num(liveRows.length + whRows.length)} tables`} />
        <StatTile label="Live pipeline" value={num(tables.live_row_total)} sub="prices + snapshots" />
        <StatTile label="Warehouse" value={num(tables.warehouse_row_total)} sub="marketdata history" />
        <StatTile label="24h net growth" value={num(delta24)} sub="rows across all tables" valueTone={delta24 >= 0 ? "good" : "critical"} />
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {splitDonut.length > 0 && (
          <Card title="Row split" subtitle="Live valuation tables vs warehouse storage." testId="ops-tables-split">
            <Donut data={splitDonut} valueFormat={(v) => String(v)} />
          </Card>
        )}
        <Card title="Prices row growth" subtitle="Append-only live price table over time." testId="ops-tables-prices-chart">
          <CountTrend data={historySeries(history, "prices")} label="Price rows" color="var(--c-s2)" />
        </Card>
      </div>

      <Card title="Live pipeline tables" subtitle="What feeds portfolio valuation and snapshots." testId="ops-tables-live">
        <Table rows={liveRows} rowKey={(r) => r.key} columns={tableCol} empty="No live table stats." />
      </Card>
      <Card title="Warehouse tables" subtitle="Historical marketdata persisted from archive endpoints." testId="ops-tables-warehouse">
        <Table rows={whRows} rowKey={(r) => r.key} columns={tableCol} empty="No warehouse table stats." />
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Candles growth" testId="ops-tables-candles-chart">
          <CountTrend data={historySeries(history, "candles")} label="Candle rows" color="var(--c-s3)" />
        </Card>
        <Card title="Transaction ticks growth" testId="ops-tables-ticks-chart">
          <CountTrend data={historySeries(history, "stock_transaction_ticks")} label="Tick rows" color="var(--c-s4)" />
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="All admin models" subtitle="Every registered table, grouped by application." testId="ops-tables-inventory-summary">
          <StatTile label="Inspectable models" value={num(inventory.length)} sub="Open a row in Django admin for records and filters" />
        </Card>
        {domainDonut.length > 0 && (
          <Card title="Models by application" testId="ops-tables-inventory-chart">
            <Donut data={domainDonut} valueFormat={(value) => num(value)} />
          </Card>
        )}
      </div>
      <Card title="Complete model inventory" subtitle="Row counts are PostgreSQL estimates; a dash means not measured. Token secrets are never shown here." testId="ops-tables-inventory">
        <Table
          rows={inventory}
          rowKey={(row) => row.admin_path}
          empty="No admin models available."
          columns={[
            { key: "model", header: "Model", render: (row) => <a href={row.admin_path} className="text-link hover:underline">{row.model}</a> },
            { key: "app", header: "Application" },
            { key: "rows_estimated", header: "Approx. rows", align: "right", render: (row) => row.rows_estimated == null ? "—" : num(row.rows_estimated) },
            { key: "bytes", header: "Size", align: "right", render: (row) => gb(row.bytes) },
          ]}
        />
      </Card>
    </div>
  );
}

function WorkflowsPanel({ overview, wf, wfPage, setWfPage }) {
  const outcomes = totalsToDonut(overview?.workflows_24h?.outcomes, {
    success: "Success",
    partial: "Partial",
    failed: "Failed",
    retry: "Retry",
    skipped: "Skipped",
    blocked_network: "Blocked network",
    blocked_storage: "Blocked storage",
  });
  const ingest = overview?.workflow_15m || {};
  const rows = wf.results || [];

  return (
    <div className="space-y-4" data-testid="ops-workflows-panel">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Runs (24h)" value={num(overview?.workflows_24h?.total_runs)} sub="All workflows" />
        <StatTile label="Rows accepted" value={num(overview?.workflows_24h?.rows_accepted)} sub={`${num(overview?.workflows_24h?.rows_rejected)} rejected`} />
        <StatTile label="Last 15 min" value={num(ingest.total_runs)} sub={`${num(ingest.rows_accepted)} accepted`} />
        <StatTile label="Failed runs" value={num(overview?.workflows_24h?.outcomes?.failed)} sub="Last 24 hours" valueTone={(overview?.workflows_24h?.outcomes?.failed || 0) > 0 ? "critical" : "good"} />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Outcomes (24h)" subtitle="Terminal state of every workflow run.">
          {outcomes.length ? <Donut data={outcomes} valueFormat={(v) => String(v)} /> : <p className="text-sm text-muted">No runs in the last 24 hours.</p>}
        </Card>
        <Card title="15-minute ingest" subtitle="Recent accepted rows by destination table." testId="ops-ingest">
          {(ingest.by_destination || []).length ? (
            <ul className="space-y-2 text-sm">
              {ingest.by_destination.map((row) => (
                <li key={row.destination_table} className="flex justify-between gap-3">
                  <span>{row.destination_table}</span>
                  <span className="tabular text-muted">{num(row.accepted)} rows · {num(row.runs)} runs</span>
                </li>
              ))}
              {/* Runs that wrote to no table, PLUS any destination past the top
                  twelve. Without this the list summed to 66 runs under a header
                  that said 126 and nothing accounted for the difference; calling
                  it "no destination" would mislabel the 13th real table. */}
              {ingest.unattributed?.runs > 0 && (
                <li className="flex justify-between gap-3 border-t border-border pt-2 text-muted">
                  <span>Other destinations</span>
                  <span className="tabular">
                    {num(ingest.unattributed.accepted)} rows · {num(ingest.unattributed.runs)} runs
                  </span>
                </li>
              )}
            </ul>
          ) : (
            <p className="text-sm text-muted">No ingest in the last 15 minutes.</p>
          )}
        </Card>
      </div>

      <Card title="Recent workflow runs" subtitle={`${num(wf.count)} total · paginated newest first.`}>
        <Table
          testId="ops-workflows"
          rows={rows}
          rowKey={(r) => r.id}
          empty="No workflow runs recorded yet."
          columns={[
            { key: "created_at", header: "When", render: (r) => dateTime(r.created_at) },
            { key: "workflow", header: "Workflow" },
            { key: "outcome", header: "Outcome", render: (r) => <Badge variant={tone(r.outcome)}>{humanize(r.outcome)}</Badge> },
            { key: "endpoint", header: "Endpoint", render: (r) => r.endpoint || "—" },
            { key: "symbol", header: "Symbol", render: (r) => r.symbol || "—" },
            { key: "rows_accepted", header: "Accepted", align: "right", render: (r) => num(r.rows_accepted) },
            { key: "error_code", header: "Error", render: (r) => r.error_code || "—" },
          ]}
        />
        <Pager page={wfPage} count={wf.count} onPage={setWfPage} />
      </Card>
    </div>
  );
}

function ErrorsPanel({ overview, wf, logPage, setLogPage }) {
  const codes = overview?.error_codes_24h || [];
  const codeDonut = codes.map((r) => ({ name: r.error_code, value: r.count }));
  const rows = wf.results || [];

  return (
    <div className="space-y-4" data-testid="ops-errors-panel">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <StatTile label="Error codes (24h)" value={num(codes.length)} sub="Distinct codes" valueTone={codes.length ? "warn" : "good"} />
        <StatTile label="Unsuccessful (page)" value={num(rows.length)} sub={`${num(wf.count)} total, incl. retries`} valueTone={wf.count ? "critical" : "good"} />
        <StatTile label="Top code count" value={codes[0] ? num(codes[0].count) : "0"} sub={codes[0]?.error_code || "none"} />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Error code distribution (24h)" testId="ops-error-codes">
          {codeDonut.length ? (
            <Donut data={codeDonut} valueFormat={(v) => String(v)} height={240} />
          ) : (
            <p className="text-sm text-muted">No error codes in the last 24 hours.</p>
          )}
        </Card>
        <Card title="Ranked codes" subtitle="Most frequent failure signatures.">
          {codes.length ? (
            <ul className="space-y-2 text-sm">
              {codes.map((row) => (
                <li key={row.error_code} className="flex items-center justify-between gap-3">
                  <code className="truncate text-xs">{row.error_code}</code>
                  <Badge variant="critical">{num(row.count)}</Badge>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">All clear for 24h.</p>
          )}
        </Card>
      </div>

      {/* `failed_only=true` selects FAILED, BLOCKED_* and RETRY, so calling this
          "failed runs" put rows badged Retry under a heading that said failure,
          next to a tile reporting 3 failures in 24h. Name what it lists. */}
      <Card
        title="Runs that did not succeed"
        subtitle="Failed, blocked, and retried runs — newest first."
      >
        <Table
          testId="ops-logs"
          rows={rows}
          rowKey={(r) => r.id}
          empty="No failed runs in this page."
          columns={[
            { key: "created_at", header: "When", render: (r) => dateTime(r.created_at) },
            { key: "workflow", header: "Workflow" },
            { key: "outcome", header: "Outcome", render: (r) => <Badge variant={tone(r.outcome)}>{humanize(r.outcome)}</Badge> },
            { key: "symbol", header: "Symbol", render: (r) => r.symbol || "—" },
            { key: "endpoint", header: "Endpoint", render: (r) => r.endpoint || "—" },
            { key: "error_code", header: "Error", render: (r) => r.error_code || "—" },
          ]}
        />
        <Pager page={logPage} count={wf.count} onPage={setLogPage} />
      </Card>
    </div>
  );
}

function ArchiveJobsPanel({
  overview,
  archives,
  archFilter,
  setArchFilter,
  selected,
  setSelected,
  confirmRetry,
  setConfirmRetry,
  retryMsg,
  doRetry,
  archPage,
  setArchPage,
}) {
  const wh = overview?.coverage?.warehouse;
  const rows = archives.results || [];

  return (
    <div className="space-y-4" data-testid="ops-archives-panel">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Failed jobs" value={num(wh?.counts?.failed)} sub={`${wh?.failed_pct ?? 0}% of ${num(wh?.total_jobs)}`} valueTone={(wh?.counts?.failed || 0) > 0 ? "critical" : "good"} />
        <StatTile label="Rows missing" value={num(wh?.counts?.partial)} sub={`${num(wh?.missing_rows)} rows short`} valueTone={(wh?.counts?.partial || 0) > 0 ? "warn" : "good"} />
        <StatTile label="Wedged" value={num(overview?.archive?.wedged)} sub="Over failure threshold" />
        <StatTile label="Missing rows" value={num(wh?.missing_rows)} sub={`${wh?.row_fill_pct ?? 0}% row fill overall`} />
      </div>

      <Card title="Retry failed archive jobs" subtitle="Select rows below, then queue a retry through the worker.">
        <div className="flex flex-wrap items-center gap-2">
          <Tabs
            label="Archive filter"
            testId="ops-arch-filter"
            value={archFilter}
            onChange={setArchFilter}
            options={[
              { value: "failed", label: "Failed only" },
              { value: "all", label: "All jobs" },
            ]}
          />
          <Button variant="primary" disabled={!selected.length} onClick={() => setConfirmRetry(true)} data-testid="ops-retry-open">
            Retry selected ({selected.length})
          </Button>
          {retryMsg && <span className="text-sm text-muted">{retryMsg}</span>}
        </div>
        {confirmRetry && (
          <div className="mt-3 rounded-lg border border-border bg-panel-2 p-3" data-testid="ops-retry-confirm">
            <p className="text-sm">Retry {selected.length} archive state(s)? This enqueues Celery jobs and writes an audit row.</p>
            <div className="mt-3 flex gap-2">
              <Button variant="primary" onClick={doRetry} data-testid="ops-retry-confirm-btn">Confirm retry</Button>
              <Button variant="ghost" onClick={() => setConfirmRetry(false)}>Cancel</Button>
            </div>
          </div>
        )}
      </Card>

      <Card title="Archive fetch states" subtitle={`${num(archives.count)} matching · newest failures first when filtered.`}>
        <Table
          testId="ops-archives"
          rows={rows}
          rowKey={(r) => r.id}
          empty="No archive states match this filter."
          columns={[
            {
              key: "pick",
              header: "",
              render: (r) => (
                <input
                  type="checkbox"
                  aria-label={`Select ${r.symbol}`}
                  checked={selected.includes(r.id)}
                  onChange={(e) => setSelected((cur) => (
                    e.target.checked ? [...cur, r.id] : cur.filter((id) => id !== r.id)
                  ))}
                />
              ),
            },
            { key: "symbol", header: "Symbol" },
            { key: "endpoint", header: "Endpoint" },
            {
              key: "state",
              header: "State",
              render: (r) => (
                <Badge variant={tone(archiveJobVariant(r))}>{humanize(archiveJobVariant(r))}</Badge>
              ),
            },
            { key: "stored", header: "Stored", align: "right", render: (r) => num(r.stored_rows) },
            { key: "missing_rows", header: "Missing", align: "right", render: (r) => num(r.missing_rows) },
            { key: "consecutive_failures", header: "Failures", align: "right", render: (r) => num(r.consecutive_failures) },
            { key: "last_attempt_at", header: "Last attempt", render: (r) => dateTime(r.last_attempt_at) },
            { key: "last_error", header: "Last error", render: (r) => r.last_error || "—" },
          ]}
        />
        <Pager page={archPage} count={archives.count} onPage={setArchPage} />
      </Card>
    </div>
  );
}


function jobsHealthStatus(overview) {
  const failed = overview?.workflows_24h?.outcomes?.failed || 0;
  const partial = overview?.workflows_24h?.outcomes?.partial || 0;
  const archFailed = overview?.coverage?.warehouse?.counts?.failed || 0;
  const archPartial = overview?.coverage?.warehouse?.counts?.partial || 0;
  const wedged = overview?.archive?.wedged || 0;
  const errorCodes = (overview?.error_codes_24h || []).length;

  if (failed > 0 || archFailed > 0 || wedged > 0) {
    return { tone: "critical", label: "Unhealthy", detail: "Failed workflow or archive jobs need attention." };
  }
  if (partial > 0 || archPartial > 0 || errorCodes > 0) {
    return { tone: "warn", label: "Degraded", detail: "Some jobs completed with gaps or recurring errors." };
  }
  return { tone: "good", label: "Healthy", detail: "No failed jobs or error signatures in the last 24 hours." };
}

function JobsHealthPanel({
  overview,
  wf,
  wfPage,
  setWfPage,
  logs,
  logPage,
  setLogPage,
  archives,
  archFilter,
  setArchFilter,
  selected,
  setSelected,
  confirmRetry,
  setConfirmRetry,
  retryMsg,
  doRetry,
  archPage,
  setArchPage,
}) {
  const status = jobsHealthStatus(overview);
  const wh = overview?.coverage?.warehouse;
  const codes = overview?.error_codes_24h || [];

  return (
    <div className="space-y-4" data-testid="ops-jobs-health-panel">
      <Card
        title="Process health"
        subtitle="Workflow runs, failure signatures, and archive backfill jobs in one place."
        testId="ops-jobs-health-summary"
        actions={<Badge variant={status.tone === "good" ? "good" : status.tone}>{status.label}</Badge>}
      >
        <p className="mb-4 text-sm text-muted">{status.detail}</p>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
          <StatTile
            label="Workflow runs (24h)"
            value={num(overview?.workflows_24h?.total_runs)}
            sub={`${num(overview?.workflows_24h?.outcomes?.failed)} failed · ${num(overview?.workflows_24h?.outcomes?.partial)} partial`}
            valueTone={(overview?.workflows_24h?.outcomes?.failed || 0) > 0 ? "critical" : (overview?.workflows_24h?.outcomes?.partial || 0) > 0 ? "warn" : "good"}
          />
          <StatTile
            label="Rows accepted (24h)"
            value={num(overview?.workflows_24h?.rows_accepted)}
            sub={`${num(overview?.workflows_24h?.rows_rejected)} rejected`}
          />
          <StatTile
            label="Error codes (24h)"
            value={num(codes.length)}
            sub={codes[0]?.error_code || "none"}
            valueTone={codes.length ? "warn" : "good"}
          />
          <StatTile
            label="Archive failed"
            value={num(wh?.counts?.failed)}
            sub={`${wh?.failed_pct ?? 0}% of ${num(wh?.total_jobs)} jobs`}
            valueTone={(wh?.counts?.failed || 0) > 0 ? "critical" : "good"}
          />
          <StatTile
            label="Archive rows missing"
            value={num(wh?.counts?.partial)}
            sub="Jobs genuinely short of rows"
            valueTone={(wh?.counts?.partial || 0) > 0 ? "warn" : "good"}
          />
          <StatTile
            label="Wedged jobs"
            value={num(overview?.archive?.wedged)}
            sub={`${num(wh?.missing_rows)} missing rows overall`}
            valueTone={(overview?.archive?.wedged || 0) > 0 ? "critical" : "good"}
          />
        </div>
      </Card>

      <WorkflowsPanel overview={overview} wf={wf} wfPage={wfPage} setWfPage={setWfPage} />
      <ErrorsPanel overview={overview} wf={logs} logPage={logPage} setLogPage={setLogPage} />
      <ArchiveJobsPanel
        overview={overview}
        archives={archives}
        archFilter={archFilter}
        setArchFilter={setArchFilter}
        selected={selected}
        setSelected={setSelected}
        confirmRetry={confirmRetry}
        setConfirmRetry={setConfirmRetry}
        retryMsg={retryMsg}
        doRetry={doRetry}
        archPage={archPage}
        setArchPage={setArchPage}
      />
    </div>
  );
}

function ResearchCoveragePanel({ coverage }) {
  if (!coverage) {
    return (
      <Card title="Research evidence coverage" testId="ops-research-coverage">
        <p className="text-sm text-muted">No completed stock-universe scan yet.</p>
      </Card>
    );
  }
  const families = [
    ["Monthly sales", "monthly_sales"],
    ["Income", "income"],
    ["Balance sheet", "balance_sheet"],
  ];
  const ageMs = Date.now() - Date.parse(coverage.finished_at);
  const stale = Number.isFinite(ageMs) && ageMs > 36 * 60 * 60 * 1000;
  return (
    <Card title="Research evidence coverage" subtitle={`${num(coverage.window_days)}-day filing window · Jalali ${coverage.start_jalali} → ${coverage.end_jalali}`} testId="ops-research-coverage">
      <p className="text-sm text-muted">Scanned {num(coverage.universe_size)} eligible TSE stocks from {dateTime(coverage.started_at)} to {dateTime(coverage.finished_at)}{stale ? " · stale scan" : ""}.</p>
      <div className="mt-3 grid gap-3 md:grid-cols-3">
        {families.map(([label, key]) => {
          const counts = coverage.summary?.[key] || {};
          return (
            <div key={key} className="rounded-lg border border-border bg-panel-2 p-3 text-sm">
              <h3 className="font-medium">{label}</h3>
              <p className="mt-2">{num(counts.symbols_with_verified)} stocks with ≥1 verified period</p>
              <p className="text-muted">{num(counts.symbols_fully_verified)} with every observed period verified</p>
              <p className="text-muted">{num(counts.withheld_periods)} withheld filing periods</p>
              <p className="text-muted">{num(counts.symbols_without_filing)} stocks with no qualifying period-end filing</p>
            </div>
          );
        })}
      </div>
      <p className="mt-3 text-xs text-muted">This uses Explore eligibility at scan time. It does not re-read archived bytes or certify peer comparisons.</p>
    </Card>
  );
}

function CodalHistoryPanel({ history }) {
  if (!history) return null;
  return (
    <Card title="Historical announcement discovery" subtitle="Source-date windows; separate from verified financial figures." testId="ops-codal-history">
      {history.leaf_windows ? (
        <>
          <div className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
            <div><strong>{num(history.symbols_started)}</strong> symbols with windows <span className="text-muted">· {num(history.catalog_stocks)} currently eligible stocks</span></div>
            <div><strong>{num(history.verified_leaf_windows)}</strong> source-reconciled windows <span className="text-muted">of {num(history.leaf_windows)} leaf windows</span></div>
            <div><strong>{num(history.open_leaf_windows)}</strong> open windows <span className="text-muted">({num(history.failed_leaf_windows)} failed)</span></div>
            <div><strong>{num(history.stale_verified_leaf_windows)}</strong> verifications older than one year</div>
          </div>
          <p className="mt-3 text-xs text-muted">
            Requested Jalali range {history.earliest_date} → {history.latest_date}; {num(history.split_parent_windows)} oversized windows split.
            Last reconciled {history.last_verified_at ? dateTime(history.last_verified_at) : "never"}.
          </p>
        </>
      ) : (
        <p className="text-sm text-muted">No historical discovery windows have been created.</p>
      )}
      <p className="mt-3 text-xs text-muted">A source-reconciled window means its returned announcement keys were stored when checked. It does not certify complete issuer history, archived documents, or parsed financial statements.</p>
      <a className="mt-2 inline-block text-sm text-link hover:underline" href="/admin/marketdata/codalhistorywindow/">Inspect source windows</a>
    </Card>
  );
}

function CodalPanel({ codal }) {
  if (!codal) return null;
  if (!codal.enabled) {
    return (
      <div className="space-y-4" data-testid="ops-codal">
        <Card title="Codal is dormant" subtitle="The subsystem is switched off; nothing is running.">
          <p className="text-sm text-muted">
            No Codal extraction worker or schedule is active. Stored announcements
            and {gb(codal.artifact_bytes)} of artifacts are kept untouched.
            Re-enable with CODAL_ENABLED=1 once the origin is reachable from the host.
          </p>
        </Card>
        <CodalHistoryPanel history={codal.history_discovery} />
        <ResearchCoveragePanel coverage={codal.research_coverage} />
      </div>
    );
  }
  const statusDonut = totalsToDonut(codal.status_counts, {});
  const blockedRate = codal.extract_runs_24h
    ? Math.round((codal.blocked_network_24h / codal.extract_runs_24h) * 100)
    : 0;

  return (
    <div className="space-y-4" data-testid="ops-codal">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile
          label="Codal worker"
          value={codal.worker_enabled ? "Enabled" : "Paused"}
          sub={codal.worker_enabled ? "Extraction consumer configured" : "Queued jobs retained; no extraction consumer"}
          valueTone={codal.worker_enabled ? "good" : "warn"}
        />
        <StatTile label="Extract runs (24h)" value={num(codal.extract_runs_24h)} sub={`${num(codal.blocked_network_24h)} blocked network`} />
        <StatTile label="Blocked rate" value={`${blockedRate}%`} sub="Network blocks / runs" valueTone={blockedRate > 10 ? "warn" : "good"} />
        <StatTile label="Artifact storage" value={gb(codal.artifact_bytes)} sub="On-disk Codal files" />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Report status mix" subtitle="CodalReport rows by parse state.">
          {statusDonut.length ? (
            <Donut data={statusDonut} valueFormat={(v) => String(v)} />
          ) : (
            <p className="text-sm text-muted">No Codal reports ingested yet.</p>
          )}
        </Card>
        <Card title="Pipeline health">
          <dl className="space-y-2 text-sm">
            <div className="flex justify-between gap-3">
              <dt className="text-muted">Last successful parse</dt>
              <dd>{codal.last_success ? dateTime(codal.last_success) : "—"}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted">Blocked network (24h)</dt>
              <dd>{num(codal.blocked_network_24h)} / {num(codal.extract_runs_24h)}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted">Queue</dt>
              <dd>{codal.worker_enabled ? "Codal worker configured" : "Codal worker paused"}</dd>
            </div>
          </dl>
        </Card>
      </div>
      <CodalHistoryPanel history={codal.history_discovery} />
      <ResearchCoveragePanel coverage={codal.research_coverage} />
    </div>
  );
}



function infraMeterTone(pct, { warn = 70, critical = 90 } = {}) {
  if (pct >= critical) return "critical";
  if (pct >= warn) return "warn";
  return "good";
}

function pctOf(part, whole) {
  const w = Number(whole);
  if (!w || w <= 0) return 0;
  return Math.min(100, Math.round((Number(part) / w) * 100));
}

const INFRA_BAR_TONE = {
  good: "bg-[var(--c-good)]",
  warn: "bg-[var(--c-warn)]",
  critical: "bg-[var(--c-critical)]",
  neutral: "bg-muted",
};

function InfraMeter({ label, valueLabel, pct, tone, sub, testId, segments, badge }) {
  const barTone = tone || infraMeterTone(pct);
  return (
    <div data-testid={testId} className="space-y-1.5">
      <div className="flex items-center justify-between gap-2 text-sm">
        <span className="text-muted">{label}</span>
        <div className="flex items-center gap-2">
          {badge}
          <span className="font-medium tabular">{valueLabel}</span>
        </div>
      </div>
      {/* Named and value-texted. Without `aria-label` a screen reader announces
          "progressbar, 62" with nothing saying what is 62% full, and
          `aria-valuenow` alone reads the percentage rather than the figure
          sighted users see beside it ("18.4 GB of 250 GB"). */}
      <div
        className="h-2.5 overflow-hidden rounded-full bg-panel-2"
        role="progressbar"
        aria-label={label}
        aria-valuenow={pct}
        aria-valuetext={valueLabel ? `${valueLabel} (${pct}%)` : undefined}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        {segments?.length ? (
          <div className="flex h-full w-full">
            {segments.map((s) => (
              <div
                key={s.key}
                className={`h-full ${s.color}`}
                style={{ width: `${Math.max(s.pct, s.pct > 0 ? 1 : 0)}%` }}
                title={s.title}
              />
            ))}
          </div>
        ) : (
          <div className={`h-full rounded-full transition-all ${INFRA_BAR_TONE[barTone]}`} style={{ width: `${Math.max(2, pct)}%` }} />
        )}
      </div>
      {sub && <p className="text-xs text-muted">{sub}</p>}
    </div>
  );
}

function InfraWorkers({ workers }) {
  const items = workers?.detail?.items || {};
  const entries = Object.entries(items);
  const online = workers?.summary?.online ?? entries.length;
  return (
    <div className="space-y-2" data-testid="ops-infra-workers">
      <div className="flex items-center justify-between gap-2 text-sm">
        <span className="text-muted">Workers</span>
        <span className="font-medium tabular">{num(online)} online</span>
      </div>
      <div className="flex flex-wrap gap-2">
        {entries.map(([name, info]) => {
          const role = name.split("@")[0] || name;
          const isOnline = info?.status === "online";
          return (
            <div
              key={name}
              className="flex min-w-[5.5rem] flex-1 items-center gap-2 rounded-lg border border-border bg-panel-2 px-2.5 py-2"
              title={name}
            >
              <span
                className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${isOnline ? "bg-[var(--c-good)]" : "bg-[var(--c-critical)]"}`}
                aria-hidden
              />
              <div className="min-w-0">
                <div className="text-xs font-medium capitalize">{humanize(role)}</div>
                <div className="truncate text-[10px] text-muted">{isOnline ? "Online" : humanize(info?.status || "offline")}</div>
              </div>
            </div>
          );
        })}
      </div>
      {entries.length === 0 && (
        <p className="text-xs text-muted">No Celery workers responding.</p>
      )}
    </div>
  );
}

// What each subscription actually buys. The plan codes alone ("tsetmc", "brs")
// do not say that one of them is the gold/FX/crypto wallet, which is the whole
// reason the two are not interchangeable.
const QUOTA_PLAN_META = {
  aio: { title: "AIO", scope: "TSETMC, Codal, and Pro daily history" },
  market_cgcc: { title: "Market CGCC", scope: "Live gold, FX, crypto, and commodities" },
};

/** One panel per provider subscription. They are separate wallets, not one pool.
 *
 * The previous version read `plan.limit` -- the ceiling the PROVIDER discloses,
 * which only rides along on error responses and is therefore 0 on an ordinary
 * day. Every consequence followed from that zero: the bar computed 0% and sat
 * empty at 6,273/10,000, the tone never left green so it could not warn, and
 * the archive/live/other segments were suppressed entirely because the code
 * guarded them behind `limit > 0`. The backend already computes
 * `effective_limit` for exactly this -- disclosed if known, else the configured
 * per-plan expectation -- and the console was throwing it away.
 *
 * A ceiling of zero is the OTHER case, and it is not exhaustion: a provider
 * product can report no daily meter, so it has no ceiling to be near.
 * Rendered against the metered layout it read
 * "475 / 0 today", a 0% meter and "Remaining 0" -- an operator's cue to go
 * looking for a wallet that had run dry, when nothing had. Unmetered wallets
 * therefore get a layout with no denominator, no meter and no remainder, and
 * keep only the usage split, which is the part that still means something.
 */
export function QuotaWallets({ quota }) {
  const plans = Object.values(quota?.plans || {});
  if (!plans.length) return <p className="text-sm text-muted">No quota data.</p>;
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2" data-testid="ops-quota-wallets">
      {plans.map((plan) => {
        const meta = QUOTA_PLAN_META[plan.plan] || { title: plan.plan, scope: "" };
        const ceiling = plan.effective_limit || 0;
        const used = plan.used || 0;
        const archive = plan.archive_used || 0;
        const live = plan.live_used || 0;
        const other = plan.other_used || 0;
        const baseline = plan.provider_baseline_used || 0;
        // `metered` is the backend's own declaration, not a guess from the
        // ceiling: a plan could in principle be metered with an unknown limit.
        const metered = plan.metered !== false;
        // Only post-observation drift is unattributed. Earlier provider usage
        // is a separate baseline, not a local bucket or a reconciliation error.
        const unattributed = plan.unattributed || 0;
        const variance = plan.provider_variance;
        const left = Math.max(0, ceiling - used);
        const pct = pctOf(used, ceiling);
        const disclosed = (plan.limit || 0) > 0;
        const meterTone = plan.blocked ? "critical" : infraMeterTone(pct, { warn: 75, critical: 92 });
        return (
          <div
            key={plan.plan}
            className="space-y-2.5 rounded-lg border border-border bg-panel-2 p-3.5"
            data-testid={`ops-quota-plan-${plan.plan}`}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="text-sm font-medium">{meta.title}</div>
                <div className="truncate text-xs text-muted">{meta.scope}</div>
              </div>
              <Badge variant={plan.blocked ? "critical" : metered ? meterTone : "neutral"}>
                {plan.blocked ? "Provider blocked" : metered ? `${pct.toFixed(0)}%` : "Unmetered"}
              </Badge>
            </div>

            <div className="flex items-baseline gap-1.5">
              <span className="text-xl font-semibold tabular">{plan.provider_used == null ? "—" : num(plan.provider_used)}</span>
              <span className="text-sm text-muted">
                {metered ? `/ ${num(ceiling)} provider-billed requests` : "provider-billed requests"}
              </span>
            </div>
            <p className="text-xs text-muted">
              {plan.provider_observed_at
                ? `Observed ${dateTime(plan.provider_observed_at)} via ${plan.provider_observation_source || "provider response"}; not live.`
                : "Provider counter not observed; billing cannot be reconciled yet."}
            </p>
            {variance != null && variance !== 0 && (
              <Badge variant={variance > 0 ? "warn" : "neutral"}>
                {variance > 0
                  ? `Unattributed since baseline +${num(variance)}`
                  : `Provider snapshot trails local by ${num(-variance)} (may be in flight)`}
              </Badge>
            )}

            {metered && (
            <div
              className="flex h-2.5 overflow-hidden rounded-full bg-panel"
              role="progressbar"
              aria-label={`${meta.title} safe-budget counter`}
              aria-valuenow={Math.round(pct)}
              aria-valuetext={`${num(used)} of ${num(ceiling)} safe-budget counter`}
              aria-valuemin={0}
              aria-valuemax={100}
            >
              <div className="h-full bg-[var(--c-s6)]" style={{ width: `${pctOf(baseline, ceiling)}%` }} title={`Pre-observation baseline ${num(baseline)}`} />
              <div className="h-full bg-[var(--c-s3)]" style={{ width: `${pctOf(archive, ceiling)}%` }} title={`Archive ${num(archive)}`} />
              <div className="h-full bg-[var(--c-good)]" style={{ width: `${pctOf(live, ceiling)}%` }} title={`Live ${num(live)}`} />
              <div className="h-full bg-muted" style={{ width: `${pctOf(other, ceiling)}%` }} title={`Other ${num(other)}`} />
              <div className="h-full bg-[var(--c-s7)]" style={{ width: `${pctOf(unattributed, ceiling)}%` }} title={`Unattributed ${num(unattributed)}`} />
            </div>
            )}

            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Local attempts</dt><dd className="tabular">{num(plan.local_attempts)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Successful</dt><dd className="tabular">{num(plan.successful_requests)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Local safe-budget count</dt><dd className="tabular">{num(used)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Before first observation</dt><dd className="tabular">{plan.provider_baseline_used == null ? "—" : num(baseline)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Archive</dt><dd className="tabular">{num(archive)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Live</dt><dd className="tabular">{num(live)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Other</dt><dd className="tabular">{num(other)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Unattributed</dt><dd className="tabular">{plan.provider_used == null ? "—" : num(unattributed)}</dd>
              </div>
              {metered && (
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">Safe allowance</dt><dd className="tabular">{num(left)}</dd>
                </div>
              )}
            </dl>

            {/* The two numbers that explain why the archive stopped while the
                wallet still had budget: quota held back for the live loop, and
                how much of the rest today's pacing has released so far. */}
            <div className="border-t border-border pt-2 text-xs text-muted">
              {metered && (
                <>
                  <div className="flex justify-between gap-2">
                    <span>Reserved for live prices</span>
                    <span className="tabular">{num(plan.live_reserve)}</span>
                  </div>
                  <div className="flex justify-between gap-2">
                    <span>Archive may spend now</span>
                    <span className="tabular">{num(plan.archive_allowance_now)}</span>
                  </div>
                </>
              )}
              <p className="mt-1.5">
                {!metered
                  ? plan.provider_observed_at
                    ? "The provider reports no daily ceiling for this product."
                    : "No daily ceiling is configured or observed; confirm with the provider."
                  : disclosed
                    ? "Ceiling disclosed by the provider."
                    : "Ceiling is our configured expectation until a provider counter is observed."}
              </p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function InfraPanel({ overview, depths, queueTotal, gb }) {
  const disk = overview.disk || {};
  const priceFeed = overview.checks?.price_feed || overview.price_feed || {};
  const priceAge = priceFeed.latest_price_age_seconds;
  const priceThreshold = priceFeed.threshold_seconds || 900;
  const pricePct = priceAge != null ? pctOf(priceAge, priceThreshold) : 0;
  const priceTone = priceFeed.status === "fresh" ? "good" : priceFeed.status === "stale" ? "warn" : "critical";
  const mail = overview.outbound_mail || overview.checks?.outbound_mail || {};

  const liveQ = Number(depths?.live || 0);
  const archiveQ = Number(depths?.archive || 0);
  const codalQ = Number(depths?.codal || 0);
  const queueSegments = queueTotal > 0 ? [
    { key: "live", pct: pctOf(liveQ, queueTotal), color: "bg-[var(--c-good)]", title: `Live queue ${num(liveQ)}` },
    { key: "archive", pct: pctOf(archiveQ, queueTotal), color: "bg-[var(--c-s3)]", title: `Archive queue ${num(archiveQ)}` },
    { key: "codal", pct: pctOf(codalQ, queueTotal), color: "bg-[var(--c-warn)]", title: `Codal queue ${num(codalQ)}` },
  ] : [];

  // The device, not our share of it. This meter used to draw the database's
  // logical size against 80% of a budget nobody had measured, so it read 10%
  // full on a disk that was 75% full.
  const diskTotal = Number(disk.filesystem_total_bytes) || 0;
  const diskUsed = Number(disk.filesystem_used_bytes) || 0;
  const diskPct = pctOf(diskUsed, diskTotal);
  const diskOurs = Number(disk.used_bytes) || 0;

  return (
    <div className="space-y-5" data-testid="ops-infra-panel">
      <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-panel-2 px-3 py-2.5">
        {/* Scoped, because the Jobs tab grades something else entirely and the
            two disagreed in the bare: this reads workers, queues, disk and the
            price feed, and said "Healthy" while "Process health" said
            "Unhealthy" about failed archive jobs. Both were right. */}
        <span className="text-sm text-muted">Workers, queues, disk &amp; feed</span>
        <Badge variant={tone(overview.status)} testId="ops-infra-overall">{humanize(overview.status)}</Badge>
      </div>

      <InfraWorkers workers={overview.workers} />

      <InfraMeter
        label="Price feed age"
        valueLabel={priceAge != null ? formatAge(priceAge) : humanize(priceFeed.status)}
        pct={pricePct}
        tone={priceTone}
        badge={<Badge variant={tone(priceFeed.status)}>{humanize(priceFeed.status)}</Badge>}
        sub={priceThreshold ? `Fresh threshold: ${num(priceThreshold)}s` : undefined}
        testId="ops-infra-price-feed"
      />

      {/* Outbound mail. A row rather than a meter: there is no percentage to
          draw, and the reason it is here at all is that this failure is
          invisible everywhere else. Password reset answers the same 200 whether
          or not the send worked -- deliberately, so it cannot enumerate
          accounts -- so an unreachable relay looks exactly like a working one
          from outside, and only a log line says otherwise. */}
      {mail.status && mail.status !== "healthy" && (
        <div
          className="flex flex-wrap items-start justify-between gap-2 rounded-lg border border-border bg-panel-2 px-3 py-2.5"
          data-testid="ops-infra-outbound-mail"
        >
          <div className="min-w-0">
            <div className="text-sm text-muted">Outbound mail</div>
            {mail.message && (
              <p className="mt-0.5 max-w-prose text-xs text-muted">{mail.message}</p>
            )}
          </div>
          <Badge variant={mail.ok ? "neutral" : "warn"}>{humanize(mail.status)}</Badge>
        </div>
      )}

      <InfraMeter
        label="Task queues"
        valueLabel={queueTotal ? `${num(queueTotal)} pending` : "All clear"}
        pct={queueTotal ? Math.min(100, queueTotal * 5) : 0}
        tone={queueTotal > 50 ? "critical" : queueTotal > 10 ? "warn" : "good"}
        segments={queueSegments}
        sub={`Live ${num(liveQ)} · Archive ${num(archiveQ)} · Codal ${num(codalQ)}`}
        testId="ops-infra-queues"
      />

      <InfraMeter
        label="Disk used"
        valueLabel={diskTotal ? `${gb(diskUsed)} of ${gb(diskTotal)}` : "Unmeasured"}
        pct={diskPct}
        tone={disk.alert ? "warn" : infraMeterTone(diskPct, { warn: 60, critical: 80 })}
        badge={disk.alert ? <Badge variant="warn">alert</Badge> : null}
        sub={[
          disk.days_to_80pct != null ? `${disk.days_to_80pct}d until 80% full` : null,
          diskOurs ? `This app ${gb(diskOurs)}` : null,
          disk.codal_bytes ? `Codal files ${gb(disk.codal_bytes)}` : null,
        ].filter(Boolean).join(" · ") || undefined}
        testId="ops-infra-disk"
      />
    </div>
  );
}


function formatAge(seconds) {
  if (seconds == null) return "—";
  const s = Number(seconds);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} hr ago`;
  return `${Math.round(s / 86400)} days ago`;
}

/**
 * Hand a locked-out user a way back in, without a mail relay.
 *
 * Password reset is mint-a-token plus deliver-it, and only delivery is broken in
 * production -- there is no SMTP relay, so the self-service flow answers "check
 * your email" and sends nothing. This exposes the half that works: an operator
 * mints the link and delivers it however they already reach that person, and the
 * user redeems it through the ordinary reset page and chooses their own
 * password. The operator never sees it.
 *
 * It confers nothing an admin lacks -- Django admin can already set any
 * password outright -- and it stops being needed the day a relay is configured.
 */
/**
 * Who has an account, and the one lever that takes it away.
 *
 * `is_active` is the entire ban mechanism in this product -- there is no
 * suspension model and no plan tier -- and until now it could only be reached
 * through Django admin, where the user form is the wrong tool: it also exposes
 * the password hash. Deactivating here additionally revokes the member's live
 * refresh tokens server-side, so the ban takes effect on the next request rather
 * than whenever their access token happens to lapse.
 *
 * Reloads the whole list after a toggle rather than patching the row in place:
 * the server is the authority on whether the change was allowed (it refuses
 * self-deactivation and the last active admin), and a locally-mutated row
 * would show a ban that did not happen.
 */
function MembersPanel() {
  const [search, setSearch] = useState("");
  const [searchDebounced, setSearchDebounced] = useState("");
  const [members, setMembers] = useState([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState(0);

  useEffect(() => {
    const id = setTimeout(() => setSearchDebounced(search.trim()), 250);
    return () => clearTimeout(id);
  }, [search]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setMembers(await adminUsers({ search: searchDebounced || undefined }));
    } catch (err) {
      setError(err?.message || "Failed to load members");
    } finally {
      setLoading(false);
    }
  }, [searchDebounced]);

  useEffect(() => {
    load();
  }, [load]);

  const toggle = async (row) => {
    setBusyId(row.id);
    setError("");
    try {
      await adminSetUserActive(row.id, !row.is_active);
      await load();
    } catch (err) {
      setError(err?.message || "Failed to update member");
    } finally {
      setBusyId(0);
    }
  };

  const columns = [
    {
      key: "email",
      header: "Member",
      render: (row) => (
        <div className="min-w-0">
          <div className="truncate text-text">{row.email}</div>
          {(row.first_name || row.last_name) && (
            <div className="truncate text-xs text-muted">
              {`${row.first_name} ${row.last_name}`.trim()}
            </div>
          )}
        </div>
      ),
    },
    {
      key: "role",
      header: "Role",
      render: (row) => (
        <Badge variant={row.role === "admin" ? "warn" : "neutral"}>
          {row.role === "admin" ? "admin" : "user"}
        </Badge>
      ),
    },
    {
      key: "is_active",
      header: "Status",
      render: (row) => (
        <Badge variant={row.is_active ? "good" : "critical"}>
          {row.is_active ? "active" : "deactivated"}
        </Badge>
      ),
    },
    { key: "accounts_count", header: "Portfolios", align: "right",
      render: (row) => num(row.accounts_count) },
    { key: "date_joined", header: "Joined", render: (row) => dateTime(row.date_joined) },
    {
      key: "last_login",
      header: "Last sign-in",
      render: (row) => (row.last_login ? dateTime(row.last_login) : "—"),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (row) => (
        <Button
          variant={row.is_active ? "ghost" : "primary"}
          disabled={busyId === row.id}
          onClick={() => toggle(row)}
          data-testid={`ops-member-toggle-${row.id}`}
          className="text-xs"
        >
          {busyId === row.id
            ? "Saving…"
            : row.is_active
              ? "Deactivate"
              : "Reinstate"}
        </Button>
      ),
    },
  ];

  return (
    <Card
      title="Members"
      subtitle="Everyone with an account. Deactivating revokes their sessions immediately."
      testId="ops-members"
      actions={
        <Button variant="ghost" className="text-xs" onClick={load} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      }
    >
      <Input
        label="Search members by email"
        className="mb-3 w-full max-w-sm"
        placeholder="Search by email…"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        data-testid="ops-members-search"
      />
      {error && (
        <p
          className="mb-3 text-xs text-[var(--c-critical-text)]"
          role="alert"
          data-testid="ops-members-error"
        >
          {error}
        </p>
      )}
      <Table
        columns={columns}
        rows={members}
        rowKey={(row) => row.id}
        empty="No members match that search."
        testId="ops-members-table"
        caption="Registered members and their access status"
      />
    </Card>
  );
}

function OperatorResetLink() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState({ status: "idle" });

  const issue = async (event) => {
    event.preventDefault();
    if (!email.trim()) return;
    setState({ status: "working" });
    try {
      const data = await adminPasswordResetLink(email.trim());
      setState({ status: "done", link: data.link });
    } catch (error) {
      setState({ status: "error", message: error.message });
    }
  };

  return (
    <form
      className="mt-4 rounded-lg border border-border bg-panel-2 p-3"
      onSubmit={issue}
      data-testid="ops-reset-link"
    >
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-0 grow">
          <label
            className="block text-xs font-medium tracking-wide text-muted uppercase"
            htmlFor="ops-reset-email"
          >
            Issue a password reset link
          </label>
          <Input
            id="ops-reset-email"
            type="email"
            autoComplete="off"
            className="mt-1 w-full"
            placeholder="person@example.com"
            label="Email address of the account to recover"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>
        <Button
          type="submit"
          variant="primary"
          disabled={state.status === "working" || !email.trim()}
        >
          {state.status === "working" ? "Issuing…" : "Issue link"}
        </Button>
      </div>
      <p className="mt-2 max-w-prose text-xs text-muted">
        Outbound mail is not configured, so self-service reset sends nothing. Mint
        a link here and deliver it yourself; it is single-use and the account
        holder chooses the password.
      </p>
      {/* Polite, not assertive: the operator submitted this deliberately and is
          already looking at it, so it should not interrupt a screen reader. */}
      <div aria-live="polite" className="mt-2">
        {state.status === "done" && (
          <div className="flex flex-wrap items-center gap-2">
            <code
              className="min-w-0 grow overflow-x-auto rounded border border-border bg-panel px-2 py-1 text-xs text-text"
              data-testid="ops-reset-link-value"
            >
              {state.link}
            </code>
            <Button onClick={() => navigator.clipboard?.writeText(state.link)}>
              Copy
            </Button>
          </div>
        )}
        {state.status === "error" && (
          <p className="text-xs text-[var(--c-critical-text)]" data-testid="ops-reset-link-error">
            {state.message}
          </p>
        )}
      </div>
    </form>
  );
}


function AttentionPanel({ liveHeld, warehouse, onNavigate }) {
  const [view, setView] = useState("live");

  // The label maps are read INSIDE each memo rather than hoisted above them.
  // `x?.status_labels || {}` mints a fresh object on every render whenever the
  // payload omits the field, so as a dependency it defeated the memo entirely
  // and both tables were rebuilt on every parent render.
  const liveRows = useMemo(() => {
    const liveLabels = liveHeld?.status_labels || {};
    return (liveHeld?.assets || [])
      .filter((a) => a.status === "stale" || a.status === "missing")
      .sort((a, b) => {
        if (a.status !== b.status) return a.status === "missing" ? -1 : 1;
        return (b.age_seconds || 0) - (a.age_seconds || 0);
      })
      .map((a) => ({
        key: a.key,
        asset: a.name || a.key,
        issue: a.status === "missing" ? "No price stored" : "Price too old",
        issueVariant: a.status === "missing" ? "critical" : "warn",
        explanation: liveLabels[a.status] || humanize(a.status),
        lastUpdate: a.status === "missing"
          ? "Never fetched"
          : a.fetched_at
            ? `${formatAge(a.age_seconds)} · ${dateTime(a.fetched_at)}`
            : formatAge(a.age_seconds),
        symbol: [a.tse_symbol, a.brs_symbol].filter(Boolean).join(" · ") || "—",
      }));
  }, [liveHeld]);

  const warehouseRows = useMemo(() => {
    const whLabels = warehouse?.status_labels || {};
    return (warehouse?.by_endpoint || [])
      .map((e) => {
        const failed = e.counts?.failed || 0;
        const partial = e.counts?.partial || 0;
        if (!failed && !partial) return null;
        const issue = failed > 0 ? "Fetch failing" : "Rows still missing";
        const statusKey = failed > 0 ? "failed" : "partial";
        return {
          key: e.endpoint,
          dataType: e.label,
          issue,
          issueVariant: failed > 0 ? "critical" : "warn",
          explanation: whLabels[statusKey] || humanize(statusKey),
          jobs: failed && partial
            ? `${num(failed)} failing · ${num(partial)} short of rows`
            : failed
              ? `${num(failed)} failing jobs`
              : `${num(partial)} jobs short of rows`,
          rowFill: e.row_fill_pct != null ? `${e.row_fill_pct}%` : "—",
          sortKey: failed * 1000 + partial,
        };
      })
      .filter(Boolean)
      .sort((a, b) => b.sortKey - a.sortKey);
  }, [warehouse]);

  const liveCount = liveRows.length;
  const whCount = warehouseRows.length;

  return (
    <Card
      title="Needs attention"
      subtitle={view === "live"
        ? "Held assets without a fresh live price (threshold: 5 minutes)."
        : "Archive data types with failed fetches or incomplete historical backfill."}
      testId="ops-overview-attention"
      actions={(
        <Button variant="ghost" className="text-xs" onClick={() => onNavigate(view === "live" ? "live" : "warehouse")}>
          Details →
        </Button>
      )}
    >
      <Tabs
        label="Attention scope"
        testId="ops-attention-scope"
        value={view}
        onChange={setView}
        options={[
          { value: "live", label: `Live prices (${liveCount})` },
          { value: "warehouse", label: `Warehouse (${whCount})` },
        ]}
      />

      {view === "live" ? (
        <div className="mt-4 space-y-3" data-testid="ops-attention-live">
          {liveCount === 0 ? (
            <p className="text-sm text-muted">All held assets have a fresh live price.</p>
          ) : (
            <>
              <p className="text-sm text-muted">
                {liveCount} held {liveCount === 1 ? "asset needs" : "assets need"} a fresh price before portfolio valuation is fully current.
              </p>
              <Table
                rows={liveRows}
                rowKey={(r) => r.key}
                columns={[
                  { key: "asset", header: "Asset" },
                  {
                    key: "issue",
                    header: "Issue",
                    render: (r) => <Badge variant={r.issueVariant}>{r.issue}</Badge>,
                  },
                  { key: "explanation", header: "What it means" },
                  { key: "lastUpdate", header: "Last update" },
                  { key: "symbol", header: "Symbol", render: (r) => <span className="font-mono text-xs text-muted">{r.symbol}</span> },
                ]}
              />
            </>
          )}
        </div>
      ) : (
        <div className="mt-4 space-y-3" data-testid="ops-attention-warehouse">
          {whCount === 0 ? (
            <p className="text-sm text-muted">No warehouse endpoints with failures or gaps.</p>
          ) : (
            <>
              <p className="text-sm text-muted">
                {whCount} archive data {whCount === 1 ? "type has" : "types have"} jobs that failed or stopped before completing backfill.
              </p>
              <Table
                rows={warehouseRows}
                rowKey={(r) => r.key}
                columns={[
                  { key: "dataType", header: "Data type" },
                  {
                    key: "issue",
                    header: "Issue",
                    render: (r) => <Badge variant={r.issueVariant}>{r.issue}</Badge>,
                  },
                  { key: "explanation", header: "What it means" },
                  { key: "jobs", header: "Affected jobs", align: "right" },
                  { key: "rowFill", header: "Row fill", align: "right" },
                ]}
              />
            </>
          )}
        </div>
      )}
    </Card>
  );
}


function OverviewPanel({ overview, tickSeries, depths, onNavigate, user }) {
  const cov = overview.coverage;
  const liveHeld = cov?.live?.held;
  const warehouse = cov?.warehouse;
  const liveDonut = totalsToDonut(liveHeld?.totals, liveHeld?.status_labels);
  const whDonut = totalsToDonut(warehouse?.counts, warehouse?.status_labels);
  const wfOutcomes = totalsToDonut(overview.workflows_24h?.outcomes, {
    success: "Success",
    partial: "Partial",
    failed: "Failed",
    retry: "Retry",
    skipped: "Skipped",
    blocked_network: "Blocked network",
    blocked_storage: "Blocked storage",
  });

  const queueTotal = Object.values(depths || {}).reduce((s, v) => s + Number(v || 0), 0);

  return (
    <div className="space-y-4" data-testid="ops-overview">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <StatTile
          label="Live prices (held)"
          value={`${liveHeld?.fresh_pct ?? 0}%`}
          sub={`${num(liveHeld?.fresh_assets)} fresh / ${num(liveHeld?.fetchable_assets)} fetchable`}
          valueTone={(liveHeld?.fresh_pct ?? 0) >= 90 ? "good" : (liveHeld?.fresh_pct ?? 0) >= 70 ? "warn" : "critical"}
          testId="ops-overview-live-fresh"
        />
        <StatTile
          label="Warehouse healthy"
          value={`${warehouse?.healthy_pct ?? 0}%`}
          sub={`${num(warehouse?.counts?.failed)} failing · ${num(warehouse?.counts?.partial)} short of rows`}
          valueTone={(warehouse?.counts?.failed || 0) > 0 ? "critical" : (warehouse?.counts?.partial || 0) > 0 ? "warn" : "good"}
          testId="ops-overview-wh-complete"
        />
        <StatTile
          label="Symbols fetched"
          value={`${warehouse?.symbol_census?.fetched_pct ?? 0}%`}
          sub={`${num(warehouse?.symbol_census?.fetched)}/${num(warehouse?.symbol_census?.symbols_total)} · ${num(warehouse?.symbol_census?.never_fetched)} never · ${num(warehouse?.symbol_census?.unfetchable)} unfetchable`}
          valueTone={(warehouse?.symbol_census?.fetched_pct ?? 0) >= 90 ? "good" : "warn"}
          testId="ops-overview-symbol-census"
        />
        <StatTile
          label="Row fill"
          value={`${warehouse?.row_fill_pct ?? 0}%`}
          sub={`${num(warehouse?.missing_rows)} missing rows`}
          testId="ops-overview-row-fill"
        />
        <StatTile
          label="179-day gate"
          value={`${liveHeld?.integrity?.passing ?? 0}/${liveHeld?.integrity?.assessed ?? 0}`}
          sub={`${num(liveHeld?.integrity?.failing)} fail · ${num(liveHeld?.integrity?.not_assessed)} not assessed`}
          testId="ops-overview-gate"
        />
        <StatTile
          label="Pipeline rows (24h)"
          value={num(overview.workflows_24h?.rows_accepted)}
          sub={`${num(overview.workflows_24h?.total_runs)} job runs · ${num(overview.workflows_24h?.rows_rejected)} rejected`}
          testId="ops-overview-pipeline-rows-24h"
        />
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card
          title="Live pipeline"
          subtitle="Held assets — what feeds portfolio valuation right now."
          testId="ops-overview-live-card"
          actions={<Button variant="ghost" className="text-xs" onClick={() => onNavigate("live")}>Details →</Button>}
        >
          {liveDonut.length ? (
            <Donut data={liveDonut} valueFormat={(v) => String(v)} testId="ops-overview-live-donut" />
          ) : (
            <p className="text-sm text-muted">No live coverage data.</p>
          )}
        </Card>
        <Card
          title="Warehouse archive"
          subtitle="Historical marketdata jobs by lifecycle state."
          testId="ops-overview-wh-card"
          actions={<Button variant="ghost" className="text-xs" onClick={() => onNavigate("warehouse")}>Details →</Button>}
        >
          {whDonut.length ? (
            <Donut data={whDonut} valueFormat={(v) => String(v)} testId="ops-overview-wh-donut" />
          ) : (
            <p className="text-sm text-muted">No warehouse coverage data.</p>
          )}
          <p className="mt-3 text-sm text-muted">
            {num(warehouse?.stored_rows)} stored / {num(warehouse?.expected_rows)} expected rows
            · {num(overview.archive?.wedged)} wedged jobs
          </p>
        </Card>
      </div>

      <Card
        title="People and books"
        subtitle="User-domain health — not warehouse ingest. Snapshot age is how recently valuation history was written."
        testId="ops-overview-users"
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatTile
            label="Users"
            value={num(overview.users?.total)}
            sub={`${num(overview.users?.active)} active · ${num(overview.users?.admins)} admins`}
            testId="ops-overview-users-total"
          />
          <StatTile
            label="With a portfolio"
            value={num(overview.users?.with_accounts)}
            sub={`${num(overview.users?.accounts)} portfolios · ${num(overview.users?.holdings)} holdings`}
          />
          <StatTile
            label="Last sign-in"
            value={overview.users?.last_login ? dateTime(overview.users.last_login) : "—"}
          />
          <StatTile
            label="Last valuation snapshot"
            value={overview.users?.last_snapshot_at ? dateTime(overview.users.last_snapshot_at) : "—"}
            sub={`${num(overview.users?.snapshots_24h)} in 24h`}
          />
        </div>
        {user?.role === "admin" && <OperatorResetLink />}
      </Card>

      {/* Admin-only roster and account recovery controls. */}
      {user?.role === "admin" && <MembersPanel />}

      <AttentionPanel liveHeld={liveHeld} warehouse={warehouse} onNavigate={onNavigate} />

      <Card
        title="Provider quota by product"
        subtitle="Provider product counters and local request attribution. Compare them before acting on remaining capacity."
        testId="ops-overview-quota"
      >
        <QuotaWallets quota={overview.quota} />
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card title="Infrastructure" testId="ops-overview-infra">
          <InfraPanel overview={overview} depths={depths} queueTotal={queueTotal} gb={gb} />
        </Card>

        <Card title="Workflow outcomes (24h)" testId="ops-overview-wf-24h">
          {wfOutcomes.length ? (
            <Donut data={wfOutcomes} valueFormat={(v) => String(v)} height={220} />
          ) : (
            <p className="text-sm text-muted">No workflow runs in the last 24 hours.</p>
          )}
          <p className="mt-2 text-xs text-muted">
            Last 15m: {num(overview.workflow_15m?.rows_accepted)} accepted · {num(overview.workflow_15m?.rows_rejected)} rejected
          </p>
        </Card>

        <Card title="Top errors (24h)" testId="ops-overview-errors">
          {(overview.error_codes_24h || []).length ? (
            <ul className="space-y-1.5 text-sm">
              {overview.error_codes_24h.map((row) => (
                <li key={row.error_code} className="flex justify-between gap-3">
                  <span className="truncate font-mono text-xs">{row.error_code}</span>
                  <span className="tabular text-muted">{num(row.count)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">No error codes recorded in the last 24 hours.</p>
          )}
          <Button variant="ghost" className="mt-3 text-xs" onClick={() => onNavigate("jobs")}>Jobs & health →</Button>
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Database growth" testId="ops-bytes-chart">
          <CountTrend data={tickSeries.bytes} label="Postgres size over time" />
        </Card>
        <Card title="Tick warehouse growth" testId="ops-tick-chart">
          <CountTrend data={tickSeries.ticks} label="Transaction tick rows" color="var(--c-s3)" />
        </Card>
      </div>
    </div>
  );
}

function pctRatio(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

export default function Ops({ user }) {
  const [tab, setTab] = useState("overview");
  const [overview, setOverview] = useState(null);
  const [overviewError, setOverviewError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [clock, setClock] = useState(() => Date.now());
  const [wf, setWf] = useState({ results: [], count: 0 });
  const [logs, setLogs] = useState({ results: [], count: 0 });
  const [archives, setArchives] = useState({ results: [], count: 0 });
  const [selected, setSelected] = useState([]);
  const [confirmRetry, setConfirmRetry] = useState(false);
  const [retryMsg, setRetryMsg] = useState("");
  const [wfPage, setWfPage] = useState(1);
  const [logPage, setLogPage] = useState(1);
  const [archPage, setArchPage] = useState(1);
  const [archFilter, setArchFilter] = useState("failed");

  const loadOverview = useCallback(async ({ force = false } = {}) => {
    setRefreshing(true);
    try {
      const data = await adminOverview(force ? { refresh: "1" } : undefined);
      setOverview(data);
      setOverviewError("");
    } catch (err) {
      setOverviewError(err?.message || "Failed to refresh overview");
    } finally {
      setRefreshing(false);
    }
  }, []);

  const loadTables = useCallback(async () => {
    try {
      if (tab === "jobs") {
        const archParams = {
          page: archPage,
          ordering: "-consecutive_failures",
        };
        if (archFilter === "failed") archParams.failed_only = "true";
        const [wfData, logData, archData] = await Promise.all([
          adminWorkflows({ page: wfPage, ordering: "-created_at" }),
          adminWorkflows({ page: logPage, ordering: "-created_at", failed_only: "true" }),
          adminArchiveStates(archParams),
        ]);
        setWf(wfData);
        setLogs(logData);
        setArchives(archData);
      }
    } catch (err) {
      setOverviewError(err?.message || "Failed to load table");
    }
  }, [tab, wfPage, logPage, archPage, archFilter]);

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    loadTables();
  }, [loadTables]);

  useEffect(() => {
    const id = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const tick = () => {
      if (document.hidden) return;
      loadOverview();
      loadTables();
    };
    const id = setInterval(tick, POLL_MS);
    const onVis = () => { if (!document.hidden) tick(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [loadOverview, loadTables]);

  const doRetry = async () => {
    setRetryMsg("");
    try {
      const res = await adminArchiveRetry(selected);
      setRetryMsg(`Queued ${res.queued?.length || 0} retries.`);
      setConfirmRetry(false);
      setSelected([]);
      loadTables();
      loadOverview({ force: true });
    } catch (err) {
      setRetryMsg(err?.message || "Retry failed");
    }
  };

  const tickSeries = useMemo(() => {
    const history = overview?.database_history || [];
    return {
      ticks: history.map((row) => ({ x: row.captured_at, y: nullable(row.counts?.stock_transaction_ticks) })),
      candles: history.map((row) => ({ x: row.captured_at, y: nullable(row.counts?.candles) })),
      bytes: history.map((row) => ({ x: row.captured_at, y: nullable(row.disk?.database_bytes) })),
    };
  }, [overview]);

  if (user?.role !== "admin") return <Navigate to="/" replace />;

  const generatedAtMs = overview?.generated_at ? Date.parse(overview.generated_at) : null;
  const ageSec = generatedAtMs != null && !Number.isNaN(generatedAtMs)
    ? Math.max(0, Math.round((clock - generatedAtMs) / 1000))
    : null;
  const depths = overview?.queues?.depths || overview?.queue?.depths || {};

  return (
    <div data-testid="ops-page">
      <PageHeader
        title="Operations center"
        subtitle="Data health at a glance — live prices, warehouse archive, and what needs fixing."
        actions={
          <div className="flex items-center gap-2">
            {ageSec != null && (
              <span className="text-xs text-muted" data-testid="ops-data-age">
                Data age: {ageSec}s
              </span>
            )}
            <Button variant="primary" onClick={() => { loadOverview({ force: true }); loadTables(); }} disabled={refreshing} data-testid="ops-refresh">
              {refreshing ? "Refreshing…" : "Refresh"}
            </Button>
          </div>
        }
      />

      {overviewError && (
        <p role="alert" className="mb-3 text-sm text-[var(--c-warn-text)]" data-testid="ops-stale-banner">
          {overviewError}. Showing last successful payload when available.
        </p>
      )}

      <Tabs
        label="Ops tables"
        testId="ops-tabs"
        value={tab}
        onChange={setTab}
        options={[
          { value: "overview", label: "Overview" },
          { value: "live", label: "Live pipeline" },
          { value: "warehouse", label: "Warehouse" },
          { value: "tables", label: "Tables" },
          { value: "asset", label: "Asset" },
          { value: "jobs", label: "Jobs & health" },
          { value: "codal", label: "Codal" },
        ]}
      />

      <div className="mt-4 space-y-4">
        {tab === "overview" && overview && (
          <OverviewPanel
            overview={overview}
            tickSeries={tickSeries}
            depths={depths}
            onNavigate={setTab}
            user={user}
          />
        )}

        {tab === "live" && overview?.coverage && (
          <LiveCoveragePanel coverage={overview.coverage} />
        )}

        {tab === "warehouse" && overview?.coverage && (
          <>
            <WarehouseCoveragePanel warehouse={overview.coverage.warehouse} />
            {/* Windows GROW: every state starts at the seed and
                `grow_tick_windows` widens it toward the clamp. This
                card used to print the seed setting alone, so it read "90d
                window" while symbols were already holding 12,000 days. */}
            <Card
              title="Tick window (intraday)"
              subtitle="Per-symbol windows widen as each symbol completes; the seed is only where they start."
              testId="ops-tick-coverage"
            >
              <p className="text-sm">
                {num(overview.tick_coverage?.complete)} verified / {num(overview.tick_coverage?.total)} jobs ({overview.tick_coverage?.progress_pct || 0}%)
              </p>
              <p className="text-sm text-muted">
                Window {num(overview.tick_coverage?.window_days_min)}–{num(overview.tick_coverage?.window_days_max)}d
                (avg {num(overview.tick_coverage?.window_days_avg)}d, seed {num(overview.tick_coverage?.window_days_seed)}d)
              </p>
              <p className="text-sm text-muted">{overview.tick_coverage?.oldest || "—"} → {overview.tick_coverage?.newest || "—"}</p>
            </Card>
          </>
        )}

        {tab === "tables" && overview && (
          <TablesPanel overview={overview} />
        )}

        {tab === "jobs" && overview && (
          <JobsHealthPanel
            overview={overview}
            wf={wf}
            wfPage={wfPage}
            setWfPage={setWfPage}
            logs={logs}
            logPage={logPage}
            setLogPage={setLogPage}
            archives={archives}
            archFilter={archFilter}
            setArchFilter={setArchFilter}
            selected={selected}
            setSelected={setSelected}
            confirmRetry={confirmRetry}
            setConfirmRetry={setConfirmRetry}
            retryMsg={retryMsg}
            doRetry={doRetry}
            archPage={archPage}
            setArchPage={setArchPage}
          />
        )}

        {tab === "asset" && <AssetInspector />}

        {tab === "codal" && overview && (
          <CodalPanel codal={overview.codal} />
        )}
      </div>
    </div>
  );
}

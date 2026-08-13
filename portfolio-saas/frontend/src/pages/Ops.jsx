import { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate } from "react-router-dom";
import {
  adminArchiveRetry,
  adminArchiveStates,
  adminAssetEvidence,
  adminAssetRecomputeIntegrity,
  adminAssetRefresh,
  adminAssetRetry,
  adminOverview,
  adminWorkflows,
} from "../api.js";
import { CountTrend, GroupedBar } from "../components/charts.jsx";
import {
  Badge,
  Button,
  Card,
  PageHeader,
  StatTile,
  Table,
  Tabs,
} from "../components/ui.jsx";
import { dateTime, humanize, num } from "../format.js";

const POLL_MS = 10000;

function statusVariant(status) {
  if (status === "healthy" || status === "fresh") return "good";
  if (status === "degraded" || status === "stale") return "warn";
  if (status === "critical") return "critical";
  return "neutral";
}

function gb(bytes) {
  if (bytes == null) return "—";
  return `${(Number(bytes) / 1024 ** 3).toFixed(2)} GB`;
}

function Pager({ page, count, pageSize = 25, onPage }) {
  const pages = Math.max(1, Math.ceil((count || 0) / pageSize));
  return (
    <div className="mt-3 flex items-center gap-2 text-sm">
      <Button disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</Button>
      <span className="text-muted">Page {page} / {pages}</span>
      <Button disabled={page >= pages} onClick={() => onPage(page + 1)}>Next</Button>
    </div>
  );
}

function claimTone(passed) {
  return passed ? "good" : "warn";
}

function AssetInspector() {
  const [query, setQuery] = useState("");
  const [lookup, setLookup] = useState("");
  const [evidence, setEvidence] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const load = async (key) => {
    const target = (key || query).trim();
    if (!target) return;
    setError("");
    setLookup(target);
    try {
      setEvidence(await adminAssetEvidence(target));
    } catch (err) {
      setEvidence(null);
      setError(err?.message || "Not found");
    }
  };

  const act = async (fn, label) => {
    if (!lookup) return;
    setBusy(label);
    setError("");
    try {
      await fn(lookup);
      setEvidence(await adminAssetEvidence(lookup));
    } catch (err) {
      setError(err?.message || `${label} failed`);
    } finally {
      setBusy("");
    }
  };

  return (
    <div data-testid="ops-asset">
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <input
          aria-label="Asset key or symbol"
          className="rounded-md border border-border bg-panel-2 px-2.5 py-1.5 text-sm"
          placeholder="kama_stock or کاما"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") load(); }}
          data-testid="ops-asset-search"
        />
        <Button variant="primary" onClick={() => load()} data-testid="ops-asset-load">Inspect</Button>
      </div>
      {error && <p role="alert" className="mb-3 text-sm text-[var(--c-warn)]">{error}</p>}
      {evidence && (
        <div className="space-y-3" data-testid="ops-asset-evidence">
          <Card title={`${evidence.identity?.name || evidence.identity?.key || lookup}`} subtitle={`key ${evidence.identity?.key || "—"} · TSE ${evidence.identity?.tse_symbol || "—"} · BRS ${evidence.identity?.brs_symbol || "—"}`}>
            <div className="flex flex-wrap gap-2">
              {(evidence.claims || []).map((c) => (
                <Badge key={c.id} variant={claimTone(c.passed)} title={c.definition} testId={`ops-claim-${c.id}`}>
                  {c.label}: {c.passed ? "yes" : "no"}
                </Badge>
              ))}
            </div>
            <p className="mt-2 text-sm text-muted">
              Live price {evidence.displayed_value?.price || "—"} {evidence.displayed_value?.unit || ""} · source {evidence.displayed_value?.source || "—"} · {evidence.displayed_value?.age_seconds != null ? `${evidence.displayed_value.age_seconds}s old` : "no as-of"}
            </p>
            {evidence.suggested_cli && (
              <p className="mt-2 text-xs text-muted" data-testid="ops-asset-cli">Repair stays CLI: <code>{evidence.suggested_cli}</code></p>
            )}
            <div className="mt-3 flex flex-wrap gap-2">
              <Button disabled={!!busy} onClick={() => act(adminAssetRetry, "retry")} data-testid="ops-asset-retry">Retry failed</Button>
              <Button disabled={!!busy} onClick={() => act(adminAssetRefresh, "refresh")} data-testid="ops-asset-refresh">Refresh series</Button>
              <Button disabled={!!busy} onClick={() => act(adminAssetRecomputeIntegrity, "recompute")} data-testid="ops-asset-recompute">Recompute gate</Button>
            </div>
          </Card>
          <Table
            testId="ops-asset-archive"
            rows={evidence.archive_states || []}
            rowKey={(r) => r.id}
            columns={[
              { key: "endpoint", header: "Endpoint" },
              { key: "verified_complete", header: "Payload verified", render: (r) => r.verified_complete ? "yes" : "no" },
              { key: "stored_rows", header: "Stored", align: "right" },
              { key: "missing_rows", header: "Missing", align: "right" },
              { key: "known_gap_rows", header: "Known gaps", align: "right" },
              { key: "consecutive_failures", header: "Failures", align: "right" },
              { key: "last_error", header: "Last error" },
            ]}
          />
          <Card title="179-day analytics gate" testId="ops-asset-gate">
            <p className="text-sm">coverage {pctRatio(evidence.integrity?.coverage_ratio)} · max gap {evidence.integrity?.max_gap_days ?? "—"} · missing {evidence.integrity?.missing_count ?? "—"}</p>
            <p className="text-sm text-muted">{evidence.integrity?.reason || "passes"}</p>
            {(evidence.integrity?.missing_dates || []).length > 0 && (
              <p className="mt-1 text-xs text-muted">Sample missing: {(evidence.integrity.missing_dates || []).join(", ")}</p>
            )}
          </Card>
          <Table
            testId="ops-asset-workflows"
            rows={evidence.workflows || []}
            rowKey={(r) => r.id}
            columns={[
              { key: "created_at", header: "When", render: (r) => dateTime(r.created_at) },
              { key: "workflow", header: "Workflow" },
              { key: "outcome", header: "Outcome" },
              { key: "endpoint", header: "Endpoint" },
              { key: "error_code", header: "Error" },
            ]}
          />
        </div>
      )}
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
  const [overviewAt, setOverviewAt] = useState(null);
  const [overviewError, setOverviewError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [wf, setWf] = useState({ results: [], count: 0 });
  const [logs, setLogs] = useState({ results: [], count: 0 });
  const [archives, setArchives] = useState({ results: [], count: 0 });
  const [selected, setSelected] = useState([]);
  const [confirmRetry, setConfirmRetry] = useState(false);
  const [retryMsg, setRetryMsg] = useState("");
  const [wfPage, setWfPage] = useState(1);
  const [logPage, setLogPage] = useState(1);
  const [archPage, setArchPage] = useState(1);

  const loadOverview = useCallback(async () => {
    setRefreshing(true);
    try {
      const data = await adminOverview();
      setOverview(data);
      setOverviewAt(new Date());
      setOverviewError("");
    } catch (err) {
      setOverviewError(err?.message || "Failed to refresh overview");
    } finally {
      setRefreshing(false);
    }
  }, []);

  const loadTables = useCallback(async () => {
    try {
      if (tab === "workflows") {
        setWf(await adminWorkflows({ page: wfPage, ordering: "-created_at" }));
      } else if (tab === "errors") {
        setWf(await adminWorkflows({ page: logPage, ordering: "-created_at", failed_only: "true" }));
      } else if (tab === "archives") {
        setArchives(await adminArchiveStates({
          page: archPage,
          ordering: "-consecutive_failures",
          failed_only: "true",
        }));
      }
    } catch (err) {
      setOverviewError(err?.message || "Failed to load table");
    }
  }, [tab, wfPage, logPage, archPage]);

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    loadTables();
  }, [loadTables]);

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
      loadOverview();
    } catch (err) {
      setRetryMsg(err?.message || "Retry failed");
    }
  };

  const tickSeries = useMemo(() => {
    const history = overview?.database_history || [];
    return {
      ticks: history.map((row) => ({ x: row.captured_at, y: Number(row.counts?.stock_transaction_ticks || 0) })),
      candles: history.map((row) => ({ x: row.captured_at, y: Number(row.counts?.candles || 0) })),
      bytes: history.map((row) => ({ x: row.captured_at, y: Number(row.disk?.database_bytes || 0) })),
    };
  }, [overview]);

  const completenessBars = useMemo(
    () => (overview?.archive?.categories || []).map((row) => ({
      name: row.label.replace("Stock ", ""),
      a: (row.progress_pct || 0) / 100,
      b: 1,
    })),
    [overview],
  );

  if (!user?.is_staff) return <Navigate to="/" replace />;

  const ageSec = overviewAt ? Math.round((Date.now() - overviewAt.getTime()) / 1000) : null;
  const depths = overview?.queues?.depths || overview?.queue?.depths || {};

  return (
    <div data-testid="ops-page">
      <PageHeader
        title="Operations center"
        subtitle="Warehouse fill, completeness, queues, and Codal — without reading worker logs."
        actions={
          <div className="flex items-center gap-2">
            {ageSec != null && (
              <span className="text-xs text-muted" data-testid="ops-data-age">
                Data age: {ageSec}s{overviewError ? " (stale)" : ""}
              </span>
            )}
            <Button variant="primary" onClick={() => { loadOverview(); loadTables(); }} disabled={refreshing} data-testid="ops-refresh">
              {refreshing ? "Refreshing…" : "Refresh"}
            </Button>
          </div>
        }
      />

      {overviewError && (
        <p role="alert" className="mb-3 text-sm text-[var(--c-warn)]" data-testid="ops-stale-banner">
          {overviewError}. Showing last successful payload when available.
        </p>
      )}

      {overview && (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatTile
            label="Overall"
            value={<Badge variant={statusVariant(overview.status)}>{humanize(overview.status)}</Badge>}
            testId="ops-status"
          />
          <StatTile
            label="Price feed"
            value={<Badge variant={statusVariant(overview.checks?.price_feed?.status)}>{overview.checks?.price_feed?.status}</Badge>}
            sub={
              overview.checks?.price_feed?.latest_price_age_seconds != null
                ? `${overview.checks.price_feed.latest_price_age_seconds}s old`
                : "no prices"
            }
            testId="ops-price-feed"
          />
          <StatTile
            label="Workers online"
            value={num(overview.workers?.summary?.online || 0)}
            sub={humanize(overview.workers?.status)}
            testId="ops-workers"
          />
          <StatTile
            label="Archive payload verified"
            value={`${overview.archive?.progress_pct || 0}%`}
            sub={`${num(overview.archive?.complete || 0)} / ${num(overview.archive?.total || 0)} jobs · not listing history`}
            testId="ops-archive-progress"
          />
        </div>
      )}

      <Tabs
        label="Ops tables"
        testId="ops-tabs"
        value={tab}
        onChange={setTab}
        options={[
          { value: "overview", label: "Overview" },
          { value: "asset", label: "Asset" },
          { value: "completeness", label: "Completeness" },
          { value: "workflows", label: "Workflows" },
          { value: "errors", label: "Errors" },
          { value: "archives", label: "Archive" },
          { value: "codal", label: "Codal" },
        ]}
      />

      <div className="mt-4 space-y-4">
        {tab === "overview" && overview && (
          <>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <Card title="179-day analytics gate" testId="ops-integrity">
                <p className="text-sm">Passing {num(overview.integrity?.passing || 0)} / {num(overview.integrity?.assessed || 0)}</p>
                <p className="text-sm text-muted">Failing {num(overview.integrity?.failing || 0)} · quarantined {num(overview.rejected?.records || 0)}</p>
              </Card>
              <Card title="Wedged archive jobs" testId="ops-wedged">
                <p className="text-sm">{num(overview.archive?.wedged || 0)} wedged · {num(overview.archive?.failed || 0)} failed</p>
                <p className="text-sm text-muted">Last archive success {overview.last_success?.archive_state || "—"}</p>
              </Card>
              <Card title="Queues" testId="ops-queue">
                {["live", "archive", "codal"].map((q) => (
                  <p key={q} className="text-sm">{q}: <strong>{num(depths[q] || 0)}</strong></p>
                ))}
              </Card>
              <Card title="Quota" testId="ops-quota">
                <p className="text-sm">{num(overview.quota?.used || 0)} / {num(overview.quota?.limit || 0)} used</p>
                <p className="text-sm text-muted">Remaining {num(overview.quota?.remaining_daily || 0)}</p>
              </Card>
              <Card title="Disk" testId="ops-disk">
                <p className="text-sm">DB {gb(overview.disk?.database_bytes)} · Codal {gb(overview.disk?.codal_bytes)}</p>
                <p className="text-sm text-muted">
                  {overview.disk?.days_to_80pct != null
                    ? `${overview.disk.days_to_80pct} days to 80% of ${overview.disk.budget_gb} GB`
                    : "Need two snapshots to project fill"}
                  {overview.disk?.alert ? " · alert" : ""}
                </p>
              </Card>
            </div>
            <Card title="Database bytes" testId="ops-bytes-chart">
              <CountTrend data={tickSeries.bytes} label="Postgres size over time" />
            </Card>
            <Card title="Tick row growth" testId="ops-tick-chart">
              <CountTrend data={tickSeries.ticks} label="Transaction tick rows" />
            </Card>
            <Card title="15-minute ingest" testId="ops-ingest">
              <p className="text-sm">
                Accepted {num(overview.workflow_15m?.rows_accepted || 0)} · Rejected {num(overview.workflow_15m?.rows_rejected || 0)} · Runs {num(overview.workflow_15m?.total_runs || 0)}
              </p>
              <ul className="mt-2 text-xs text-muted">
                {(overview.workflow_15m?.by_destination || []).map((row) => (
                  <li key={row.destination_table}>{row.destination_table}: {num(row.accepted)}</li>
                ))}
              </ul>
            </Card>
          </>
        )}

        {tab === "completeness" && overview && (
          <>
            <Card title="Archive fill by endpoint" testId="ops-completeness">
              <GroupedBar data={completenessBars} labels={["Complete", "Target"]} label="Archive completeness" />
            </Card>
            <Card title="Tick window" testId="ops-tick-coverage">
              <p className="text-sm">
                {overview.tick_coverage?.window_days}d window: {num(overview.tick_coverage?.complete)} / {num(overview.tick_coverage?.total)} ({overview.tick_coverage?.progress_pct || 0}%)
              </p>
              <p className="text-sm text-muted">{overview.tick_coverage?.oldest || "—"} → {overview.tick_coverage?.newest || "—"}</p>
            </Card>
            <Table
              testId="ops-fill-rates"
              rows={overview.database_rows || []}
              rowKey={(r) => r.key}
              columns={[
                { key: "label", header: "Table" },
                { key: "count", header: "Rows", align: "right", render: (r) => num(r.count) },
                { key: "delta_24h", header: "24h", align: "right", render: (r) => (r.delta_24h == null ? "—" : num(r.delta_24h)) },
                { key: "delta_7d", header: "7d", align: "right", render: (r) => (r.delta_7d == null ? "—" : num(r.delta_7d)) },
              ]}
            />
          </>
        )}

        {tab === "workflows" && (
          <>
            <Table
              testId="ops-workflows"
              rows={wf.results || []}
              rowKey={(r) => r.id}
              columns={[
                { key: "created_at", header: "When", render: (r) => dateTime(r.created_at) },
                { key: "workflow", header: "Workflow" },
                { key: "outcome", header: "Outcome" },
                { key: "endpoint", header: "Endpoint" },
                { key: "symbol", header: "Symbol" },
                { key: "rows_accepted", header: "Accepted", align: "right" },
                { key: "error_code", header: "Error" },
              ]}
            />
            <Pager page={wfPage} count={wf.count} onPage={setWfPage} />
          </>
        )}

        {tab === "errors" && (
          <>
            <Card title="Error codes (24h)" testId="ops-error-codes">
              <ul className="text-sm">
                {(overview?.error_codes_24h || []).map((row) => (
                  <li key={row.error_code}>{row.error_code}: {num(row.count)}</li>
                ))}
              </ul>
            </Card>
            <Table
              testId="ops-logs"
              rows={wf.results || []}
              rowKey={(r) => r.id}
              columns={[
                { key: "created_at", header: "When", render: (r) => dateTime(r.created_at) },
                { key: "workflow", header: "Workflow" },
                { key: "outcome", header: "Outcome" },
                { key: "symbol", header: "Symbol" },
                { key: "error_code", header: "Error" },
              ]}
            />
            <Pager page={logPage} count={wf.count} onPage={setLogPage} />
          </>
        )}

        {tab === "archives" && (
          <div>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Button variant="primary" disabled={!selected.length} onClick={() => setConfirmRetry(true)} data-testid="ops-retry-open">
                Retry selected ({selected.length})
              </Button>
              {retryMsg && <span className="text-sm text-muted">{retryMsg}</span>}
            </div>
            {confirmRetry && (
              <Card className="mb-3" testId="ops-retry-confirm">
                <p className="text-sm">Retry {selected.length} failed archive state(s)? This enqueues jobs and writes an audit workflow row.</p>
                <div className="mt-3 flex gap-2">
                  <Button variant="primary" onClick={doRetry} data-testid="ops-retry-confirm-btn">Confirm retry</Button>
                  <Button variant="ghost" onClick={() => setConfirmRetry(false)}>Cancel</Button>
                </div>
              </Card>
            )}
            <Table
              testId="ops-archives"
              rows={archives.results || []}
              rowKey={(r) => r.id}
              columns={[
                {
                  key: "pick",
                  header: "",
                  render: (r) => (
                    <input
                      type="checkbox"
                      checked={selected.includes(r.id)}
                      onChange={(e) => setSelected((cur) => (
                        e.target.checked ? [...cur, r.id] : cur.filter((id) => id !== r.id)
                      ))}
                    />
                  ),
                },
                { key: "symbol", header: "Symbol" },
                { key: "endpoint", header: "Endpoint" },
                { key: "missing_rows", header: "Missing", align: "right" },
                { key: "consecutive_failures", header: "Failures", align: "right" },
                { key: "last_error", header: "Last error" },
              ]}
            />
            <Pager page={archPage} count={archives.count} onPage={setArchPage} />
          </div>
        )}

        {tab === "asset" && <AssetInspector />}

        {tab === "codal" && overview && (
          <Card title="Codal pipeline" testId="ops-codal">
            <p className="text-sm">{overview.codal?.enabled ? "Extraction enabled" : "Extraction disabled (local default; VPS enables this)"}</p>
            <p className="text-sm text-muted">Last parse: {overview.codal?.last_success || "—"}</p>
            <p className="text-sm text-muted">
              blocked_network 24h: {num(overview.codal?.blocked_network_24h || 0)} / {num(overview.codal?.extract_runs_24h || 0)}
            </p>
            <ul className="mt-2 text-sm">
              {Object.entries(overview.codal?.status_counts || {}).map(([status, count]) => (
                <li key={status}>{status}: {num(count)}</li>
              ))}
            </ul>
          </Card>
        )}
      </div>
    </div>
  );
}

# Warehouse snapshot

**When:** 2026-08-11 ~11:00 UTC (post-`f293450` verification)
**Source:** Docker Compose Redis/Postgres runtime checks after rebuild + 15-minute monitor + 6-minute confirmation
**Commit under test:** `f293450` (plus local one-line Celery `visibility_timeout` fix; see below)
**Units:** TSE warehouse and portfolio stock quotes = Rial under the legacy one-tenth-share convention; other portfolio values = Toman.

## Current state

| Metric | Value |
|---|---:|
| Archive jobs complete | 7,344 / 8,403 (87.4%) |
| Tick jobs complete | 534 / 1,366 (39.1%) |
| Remaining archive missing rows | 12,858 |
| Wedged states (`consecutive_failures >= 5`) | 51 (down from 59 at baseline) |
| Quota used (Tehran day) | 75 / 9,800 (0.8%) |
| Quarantined records | 22,489 |
| Codal Redis pending | 0 |
| Codal reports `fetching` / `blocked_network` | 631 / 1,342 |
| Market index rows | 39 |
| ETF NAV / option contract rows | 0 / 0 |

Archive state count rose versus the 2026-08-10 snapshot (8,105 → 8,403) because `ensure_archive_states` added newly eligible symbols during this run. Tick completion did not advance in this daytime window; most remaining tick work is soft-deferred or wedged on volume mismatches / missing prerequisites.

## Post-`f293450` queue verification

### First 15-minute window (pre-fix)

| Signal | Observed |
|---|---|
| `live` pending | 0–11 (brief spike while producers ran; ended at 0) |
| `archive` pending | grew to **10** and stayed elevated |
| `codal` pending | 0–1 (no refill storm) |
| Archive worker health | intermittent **unhealthy** (Celery inspect ping timeouts) |
| Backpressure | `archive_tick` correctly skipped with `queue_full` once depth ≥ 4 |

**Root cause:** Redis broker `visibility_timeout` was 300s. Long nightly jobs started on cold restart (`nightly_data_integrity`, `nightly_series_validation`, `nightly_asset_metrics`) were still running when Redis redelivered the same task ids (`redelivered: true`). Duplicates filled the archive queue above the claim cap and starved `run_archive_state`.

### Fix applied

[`config/celery.py`](../backend/config/celery.py): `visibility_timeout` **300 → 3600**. Stuck archive list purged; workers restarted.

### Confirmation window (6 minutes)

| Sample | live | archive | codal | archive complete | all workers |
|---|---:|---:|---:|---:|---|
| start | 0 | 2 | 0 | 7,332 | healthy |
| end | 0 | 2 | 0 | 7,341 | healthy |

Archive pending stayed within the claim cap (≤ 4) and cleared; live stayed 0; archive completion advanced by 9 states; no redelivery pile-up.

## Data-fill follow-ups (not queue bugs)

| Gap | Cause | Status |
|---|---|---|
| Codal `blocked_network` | Live Codal CDN `ReadTimeout` on excel/html/pdf; enqueue cap works (Redis pending ≤ 1) | Operational / network — retry later when Codal is reachable |
| Tick % stalled | Soft-defer + wedged tick mismatches; 1% tolerance already in validation | Progress expected over longer windows; wedged count fell 59 → 51 |
| Empty ETF NAV table | Live provider returns 316 ETFs (`l18`,`pl`,`bubble_percent`,…) but **no `date`/`d`**; ingest skips every row | Live-path schema bug (separate from archive queues) |
| Empty options table | Live provider returns 1,153 contracts; ingest requires `date` which the live payload does not supply (`date_begin`/`date_end` only) | Live-path schema bug |
| Market index (39 rows) | Index probe runs only while TSE is `OPEN` (session 08:30–13:00 Tehran); verification ran after close | Expected off-hours; not archive starvation |

## Active safeguards

| Issue | Control |
|---|---|
| Scheduler starvation | Producers run on `live`; generated work stays on `archive`/`codal` |
| Queue growth | Pending caps: archive 4, Codal 1; broker inspection fails closed |
| Long-task redelivery | Broker `visibility_timeout` = 3600s |
| Tick volume noise | 1% relative tolerance before quarantine |
| Hard tick mismatch loop | Rejected dates become known gaps and are not re-requested |
| Missing prerequisites | Tick/adjusted states soft-defer until their source history exists |
| Quota thrash | Exhausted archive work defers to next Tehran midnight |
| Network failures | Existing transient retry/backoff remains active |

Detailed unit policy: [`F1_POLICY.md`](F1_POLICY.md).

## Prior snapshot (2026-08-10)

| Metric | Value |
|---|---:|
| Archive jobs complete | 7,317 / 8,105 (90.3%) |
| Tick jobs complete | 532 / 1,310 (40.6%) |
| Codal Redis queue | 1,064 draining |

That earlier window verified producer routing (`live` control vs work queues) after commit `e9e517f`.

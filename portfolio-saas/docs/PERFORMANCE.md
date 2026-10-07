# Performance: how latency is measured, and what was changed

## Measuring

Every number below comes from the `perf` app (`backend/perf/`). Nothing is
stored per request — only hourly totals, so the table stays small under the
storage policy.

| Source | What it times | Where it's measured |
|---|---|---|
| `api` | Each `/api/` request, start to finish, plus SQL query count, SQL time, and how many other requests the process was serving | `PerfMiddleware`, on the server |
| `client_api` | Each `api()` call as the browser saw it, including the network and nginx queueing | `frontend/src/perf.js` |
| `client_page` | From a route change until every initial data load on that page has finished (including waterfalls) | `frontend/src/perf.js` |
| `client_boot` | From a hard reload until the first page is ready (bundle, then refresh, then `/me`, then the page) | `frontend/src/perf.js` |

Every row is tagged with the **market state**: `open`, `closed_daytime` or
`overnight`. Reports also split by **Tehran trading day vs Thu/Fri weekend** and
by **Tehran hour of day**, so you compare like with like.

### Reading it

```bash
# last 7 days, server side, worst routes by total time spent
docker compose exec backend python manage.py perf_report --flush --days 7 --source api

# what users actually waited for, per page
docker compose exec backend python manage.py perf_report --days 7 --source client_page

# before / after a deploy: run the same window either side of it
docker compose exec backend python manage.py perf_report --since 2026-10-01 --until 2026-10-06
docker compose exec backend python manage.py perf_report --since 2026-10-06 --until 2026-10-11
```

The same data as JSON for staff: `GET /api/perf/report/?days=7&source=api`
(`since`, `until`, `limit` also work).

- Percentiles come from a fixed histogram. "p95 <= 750" means the 95th
  percentile fell in the bucket that tops out at 750 ms.
- `avg_inflight` is how many other requests the same worker process was
  handling when this one started. If it rises along with latency, the problem
  is contention, not the route itself.
- Any request slower than `PERF_SLOW_REQUEST_MS` (default 1000) also writes a
  WARNING `perf {...}` log line with its request id. Grep the backend logs for
  `perf {`. `PERF_LOG_ALL_REQUESTS=1` logs every request instead.
- Every API response carries a `Server-Timing` header (`app;dur=…, db;dur=…`),
  which browser devtools show in the Timing tab.

## Changes made (2026-10-05)

| Change | Problem it fixes |
|---|---|
| The fetch task overwrites the price cache instead of deleting it; a miss is rebuilt by one request while others wait for its result | Every 20 s while the market was open, the cache was deleted, and every request in the next window rebuilt the market-wide price map at the same time |
| Postgres connection pool in the API process (`DB_POOL_MAX_SIZE=8`) | `DB_CONN_MAX_AGE=0` meant a fresh TCP + auth + backend fork on every request |
| Token refresh and CSRF bootstrap get their own `session` throttle (300/min per IP) | They shared the 30/min anonymous bucket, so a few people behind one NAT tripped it and the client read the 429 as "signed out" |
| The index benchmark loader is bounded to the window, reads 3 columns, and is cached under the table's newest id | It read the whole index table about 25 times per Risk request, and hundreds of times for MyOptimal |
| Transaction list builds `is_latest_for_asset` in one query; valuation fetches liabilities with their asset | Both ran one query per row |
| Every page is its own lazy chunk | All pages and echarts (634 kB) loaded on every signed-in route, including the ledger |
| Identical in-flight GETs share one request; polls never stack; a 429 on refresh retries once | Duplicate valuation and asset calls per page; slow responses piled up behind the 60 s poll |
| The returns matrix is versioned on the rows it actually reads (`_returns_version`; each entry also checks its own live-tick inputs via `_entry_is_current`) | Every live tick of any held stock rebuilt every risk, frontier and optimization matrix from the full history, even though a stock with a warehouse series takes no input from live ticks |

## Changes made (2026-10-07): tab and range switches

| Change | Problem it fixes |
|---|---|
| `_load_price_panel` reads the warehouse only from the window's cutoff day (`_BOUNDED_HISTORY_READS`). The bounded and full reads are pinned equal by a test across windows, as-of dates, dollar-quoted gold and FX flat runs | Every history build (the benchmark tab, Risk, Compare, the analytics matrices) read each symbol's full ~12-year history to draw a 30-day window. Measured locally on a 12-year, 2-asset panel: 180–207 ms became 24–36 ms |
| `portfolio.models.newest_prices()` replaces the `DISTINCT ON (asset_id)` latest-tick reads (valuation, price map, Ops assets) with one indexed probe per asset | PostgreSQL has no skip scan, so each read walked every tick in the 14-day window. Measured locally on 800k interleaved ticks: 366 ms became 2.6 ms, same rows. Production `/valuation/` spent 240 of 335 ms in the database, and Ops assets took 12 s |
| Integrity scoring, daily-bar rejections, the USD/USDT basis rates and the index closes read only their window | Each loaded full history to use a 180-day, in-window, 5-day or one-row-per-day slice |
| `/snapshots/?include_real=1` embeds the `real_toman` series in the nominal response; the Dashboard "vs inflation" mode uses it | That mode sent a second full snapshot request (live valuation plus the hidden-holding replay) only to divide by CPI. On a nominal basis it now costs no request at all |

## Known, not yet changed

- **`optimize()` and MyOptimal still rotate on every tick of a held asset.**
  This is deliberate, because their output includes current weights at live
  prices. Since 2026-10-05 the returns matrix underneath them is cached exactly:
  `_returns_version` only rotates on rows the matrix actually reads. So a tick
  now costs a solve, not a rebuild of years of history.
- `value_account`, `/performance`, `/snapshots` and `/data-quality` cache
  nothing per user.
- `ANALYTICS_MAX_CONCURRENT_GLOBAL=5`: about three users on heavy pages at once
  start getting 429s.

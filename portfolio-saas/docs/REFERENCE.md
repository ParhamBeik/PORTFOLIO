# Reference

Durable facts that do not belong in a code comment: the price-unit policy, the
standing warehouse safeguards, and the provenance of the recovered family
holdings. Point-in-time operational snapshots are deliberately *not* kept here —
the Ops console and the workflow ledger are the live answer.

---

## 1. Price units (the F1 policy)

**Status:** provider and warehouse unit verified as Rial · **Verified:** 2026-08-06
· **Code constant:** `marketdata.currency.TSE_PRICE_UNIT = "rial"`

### Active boundaries

1. BrsApi TSE history, candles, and transaction ticks are stored provider-verbatim in Rial.
2. Analytics panels that combine TSE data with Toman assets convert through `tse_close_to_toman()`.
3. Portfolio TSE prices currently stay Rial because recovered stock quantities use one tenth of broker share counts; numerically, `quantity × Rial price` matches the intended Toman value.
4. Gold, FX, manual assets, snapshots, liabilities, and aggregate totals remain Toman.

### Known ceiling

The one-tenth-share convention is legacy-data compatibility, not a general
multi-user unit model. Before accepting ordinary broker share counts, migrate
existing TSE holdings and ledger quantities by 10, restore TSE `portfolio_price`
to Toman, and update the boundary tests together.

### Fail-safe

If `TSE_PRICE_UNIT` returns to an unverified value, mixed TSE/non-TSE
optimization must fail closed through `MixedUnitUniverseBlocked`.

---

## 2. Warehouse safeguards

Each row is a failure mode that has actually occurred and the control now
standing against it.

| Issue | Control |
|---|---|
| Scheduler starvation | Producers run on `live`; generated work stays on `archive`/`codal` |
| Queue growth | Pending caps from `MARKETDATA_ARCHIVE_QUEUE_LIMIT` / `CODAL_EXTRACT_BATCH_SIZE` (`_queue_slots`); broker inspection fails closed |
| Long-task redelivery | Broker `visibility_timeout` = 3600s (`config/celery.py`) — at the old 300s, cold-restart nightly jobs were still running when Redis redelivered them, and the duplicates starved `run_archive_state` |
| Tick volume noise | 1% relative tolerance before quarantine |
| Hard tick mismatch loop | Rejected dates become known gaps and are not re-requested |
| Missing prerequisites | Tick/adjusted states soft-defer until their source history exists |
| Quota thrash | Exhausted archive work defers to next Tehran midnight |
| Network failures | Transient retry/backoff in `marketdata/fetchers.py` |

Known live-path schema gaps, distinct from archive health: the ETF NAV and
option-contract payloads carry no `date`/`d` field (options give only
`date_begin`/`date_end`), so ingest skips every row. Market-index rows only
accrue while the TSE is `OPEN` (08:30–13:00 Tehran).

---

## 3. Recovered Father / Mother holdings

**Recovered:** 2026-08-09. Cross-checked against the Postgres dump
`PORTFOLIO_BACKUPS/portfolio-20260807-210843.dump` (`portfolio_account`
Mother/Father plus `portfolio_holding`, imported 2026-08-03) and the legacy
`Portfolio_Tracker_v2.xlsx` tracker (Dashboard/Allocation/Live_Data at 2026-07-18
prices; the workbook was removed from this repo on 2026-08-21 and remains in git
history). `current_state.json` was always gitignored, so git history never held
quantities.

Swiss bars and pre-86 quarters were not in the SaaS asset catalog at import time,
so they are missing from the dump; the numbers below are the unique integer
solution that makes Mother's gold match the Excel Allocation total exactly at
Live_Data prices. Valuation check at those prices (Emami 188,510,000 … KAMA 2,807,
USD 193,400): Mother **12,172,999,210** / Father **11,814,530,000** — matches
Dashboard.

```json
{
  "Father": {
    "emami_coin": 21,
    "half_coin": 1,
    "quarter_coin": 3,
    "usd_cash": 3000,
    "kama_stock": 2500000
  },
  "Mother": {
    "emami_coin": 1,
    "quarter_coin": 8,
    "quarter_coin_pre86": 2,
    "one_gram_coin": 4,
    "swiss_gold_bar_1g": 4,
    "swiss_gold_bar_2_5g": 5,
    "gold_18k_gram": 165,
    "usd_cash": 500,
    "house_price_per_sqm_million": 90
  }
}
```

House constants (from archived settings): `house_area_sqm=90.2`,
`house_mortgage_deduction=400_000_000`.

## 4. Measuring latency

The `perf` app stores hourly totals only, never one row per request. Sources:
`api` (server, per `/api/` request, with SQL count/time and in-flight load),
`client_api`, `client_page` and `client_boot` (browser, `frontend/src/perf.js`).
Every row is tagged with market state (`open` / `closed_daytime` / `overnight`)
and Tehran trading day vs Thu/Fri weekend, so compare like with like.

```bash
python manage.py perf_report --flush --days 7 --source api     # worst server routes
python manage.py perf_report --days 7 --source client_page     # what users waited for
python manage.py perf_report --since 2026-10-01 --until 2026-10-06   # before/after a deploy
```

Staff JSON: `GET /api/perf/report/?days=7&source=api`. Percentiles come from a
fixed histogram ("p95 <= 750" = the bucket topping out at 750 ms). Rising
`avg_inflight` alongside latency means contention, not the route. Requests over
`PERF_SLOW_REQUEST_MS` (1000) log a `perf {...}` WARNING; every response carries
a `Server-Timing` header.

## 5. Operator commands and switches

| Feature | How to run / enable | What to know |
|---|---|---|
| Balance-sheet backfill | `backfill_balance_sheets --symbol فولاد --limit 20 --fetch [--dry-run]` | `--fetch` permits a Codal request; without it only archived sheets are parsed. Parser accepts only observed V9 templates and checks assets = liabilities + equity. Not yet run in production. |
| Codal history discovery | `backfill_codal_history --start-year 1402 --end-year 1404 [--symbols …] --max-requests 50 --execute` | Read-only without `--execute`. Windows split until ≤ `--max-pages`; complete only when provider count = stored keys. Quota-paced; not scheduled. |
| AI research (GapGPT) | On the VPS: `python3 deploy/sync_gapgpt_config.py`, then set `GAPGPT_CONFIG_HOST_FILE`, `RESEARCH_MAX_RUN_USD`, `RESEARCH_DAILY_BUDGET_USD` in `.env.production` | Provider key and tariff come from the News deployment, never Git. The model only picks observation ids; the server computes every value. Budget is reserved before each call; restart the backend after a key rotation. |
| Research coverage scan | Celery beat, daily 04:30 Tehran | Counts what Explore can show per stock over the last 365 days of filings. Reads stored evidence only; no provider calls. Ops marks it stale after 36 h. |

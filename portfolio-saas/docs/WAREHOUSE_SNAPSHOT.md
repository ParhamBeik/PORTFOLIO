# Warehouse snapshot (measured)

**When:** 2026-08-07 ~14:08 UTC (کاما repair verified ~14:25 UTC)  
**Source:** `docker exec portfolio-saas-db-1 psql` (exact `COUNT(*)`, not estimates) + BrsApi History/Candlestick spot checks  
**Policy reference:** `marketdata/currency.py` module docstring  
**Path:** `portfolio-saas/docs/WAREHOUSE_SNAPSHOT.md`

## portfolio_price (app unit: Toman / IRT)

| Metric | Value |
|---|---|
| Rows | 6,141+ (advancing) |
| `price_unit=IRT` verified | ~5,363 |
| `UNKNOWN` / empty | 778 |
| `IRR` | 0 |
| `max(fetched_at)` | advancing while Celery live is up |

Live sample (closed daytime): gold/coins/`usd_cash` written as **IRT** verified at Toman magnitudes.

## marketdata_* tables

| Table | Rows | Distinct symbols | Unit (policy + measured) |
|---|---:|---:|---|
| `marketcandle` | 3,512,384 | 1,246 | **Rial** (provider-verbatim) |
| `dailystockhistory` | 3,567,655 | 1,219 | **Rial** |
| `stocktransactiontick` | 14,439,688 | 480 | **Rial** |
| `goldcurrencyhistory` | 129,777 | 38 | `تومان` 114,569 · `دلار` 14,196 · `تتر` 1,012 |
| `cryptohistory` | 17,815 | 5,088 | USD + Toman columns both filled |
| `commodityhistory` | 84 | 14 | provider-native |
| `codalannouncement` | 73,324 | 757 | n/a |
| `shareholderrecord` | 47,929 | 1,216 | n/a |
| `marketindexdata` | 2→growing | — | snapshot rows (TEDPIX) |

Spot checks:

- کاما daily `1405-05-12` `pc=3510` **Rial**; after repair `1405-05-05/06` = **3138** (matches BrsApi)
- `USDT_IRT` / `USD` latest closes ~187–192k **تومان**

## کاما unit anomaly — fixed 2026-08-07

**Was:** 173+ `dailystockhistory` rows with `pc < 500` next to Rial-scale ~3xxx neighbors (e.g. `3138 → 313.8 → 3232`). Candles shared the same ÷10 plateaus.

**Root cause:** warehouse values at **1/10** of BrsApi `History.php` / Candlestick closes (confirmed row-by-row against the provider).

| Action | Result |
|---|---|
| `DailyStockHistory` vs History.php type=0 | **427** rows ×10 (idempotent); leftover `1403-05-17` ×10 from neighbors → **0** rows with `pc < 500` |
| `MarketCandle` 1d_adj / 1d_unadj | **9016** rows overwritten from Candlestick.php; fill dates scaled where ~10× vs nearest history |

Remaining candle closes `< 500` on adjusted series match the provider 1:1 (genuine corporate-action-adjusted history; unadjusted provider has **0** closes `< 500`).

## ArchiveFetchState completeness (same window)

| Endpoint | Complete | Incomplete | Never attempted |
|---|---:|---:|---:|
| stock_transaction_ticks | 106 | 1201 | ~1124 (shrinking slowly; many blocked until candles exist) |
| stock_history_adjusted | ~491 | ~816 | ~89 |
| stock_* candle/history unadj | majority complete | ~89–110 | ~89 |
| shareholder / codal | mostly complete | low tens | low tens |
| **market_index_daily** | **verified after import fix** | 0 | was 1; TEDPIX backfill succeeded |
| gold_daily | 39/39 | 0 | 0 |

## Celery (observed)

- Archive fan-out: `archive_tick` enqueues 12; concurrency 4 workers process in parallel.
- Live cadence: beat every 60s; Redis gate skips until interval elapses.
- After recreate: `MARKETDATA_LIVE_INTERVAL_DAYTIME/OVERNIGHT=300`, `OPEN=120`.
- `MarketIndexData` import fixed → `Successfully backfilled TEDPIX (market_index_daily)`.

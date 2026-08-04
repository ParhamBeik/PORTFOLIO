# F1 Evidence — TSE Rial vs Toman (2026-08-04)

## Verdict

**Unresolved.** No authoritative exchange confirmation in this environment. Do **not** apply a 10× conversion.

## Facts (code)

| Path | Conversion |
|---|---|
| `ingest_candles` | Stores `open/high/low/close` raw — **no** `to_toman()` |
| `ingest_daily_history` | Stores `pl`/`pc`/etc. raw — **no** `to_toman()` |
| `ingest_gold_currency_history` | Calls `to_toman(symbol, price, raw_unit)` when unit ∈ {ریال, rial, irr} |
| `MarketCandle` / `DailyStockHistory` | **No** unit column |
| `Asset.currency` for `kama_stock` | `IRT` (Toman declaration) |

`to_toman` lives in `marketdata/currency.py` and divides by 10 only when the unit string is Rial.

## Facts (database samples, Docker `portfolio`)

- کاما `1d_adj` 1405-05-10 close ≈ 3320
- USD 1405-05-10 close ≈ 194215 with `unit=تومان`
- Same-day magnitudes are consistent with either “TSE in Toman near thousands” or “TSE in Rial near thousands while FX is Toman near hundreds of thousands” — magnitude alone does not decide.

## Facts (audit artifacts)

From committed `docs/data-verification/REPORT.md` / `secondary_sources.json`:

- Internal identity `tval/tvol == pc` holds → price and trade value share one unit (does not reveal which).
- shakhesban `PDrCotVal` matched stored کاما live price; same field family on USD pages is Rial-labelled → **suggests** TSE may be Rial (corroborating, not conclusive).
- tsetmc.com was unreachable during original audit.

## Network re-probe (this stage)

| Source | Result |
|---|---|
| `cdn.tsetmc.com` ClosingPrice API | Connection timeout |
| shakhesban stock page | HTTP 410 |

Still no exchange-authoritative unit label.

## Consumers that mix TSE with Toman series

- `portfolio/services/valuation.py` — `get_latest_prices` / `value_account` / `value_as_of`
- `portfolio/services/returns.py` — `daily_returns_matrix` joins TSE candles + gold/FX
- `portfolio/services/optimization.py` — `optimize` / frontier on that matrix
- Performance / analytics views that call the above

## Blast radius if TSE is Rial and treated as Toman

Equity values and weights vs gold/FX/cash are ~**10× understated**. Same-asset-class TSE-only relative weights remain consistent.

## Blast radius if TSE is already Toman

No conversion needed; isolation policy is conservative (may block some mixed optimizations until verified).

## What would change under each interpretation

**If proven Rial:** call `to_toman(..., "rial")` in `ingest_candles` and `ingest_daily_history`; bounded historical backfill (×0.1) with manifest; set `TSE_PRICE_UNIT = "rial"`; revalidate valuation/optimization.

**If proven Toman:** set `TSE_PRICE_UNIT = "toman"`; leave stored values; lift mixed-universe guard.

**Until proven:** see `F1_POLICY.md` — isolate mixed-unit optimization; label TSE valuations; no silent conversion.

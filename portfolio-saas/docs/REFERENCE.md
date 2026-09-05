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

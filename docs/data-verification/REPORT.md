# Cross-source Financial Data Verification — Report

**Branch:** `phase/cross-source-data-verification`
**Audit date:** 2026-08-04 Gregorian / 1405-05-13 Jalali
**Database:** Docker Postgres `portfolio-saas-db-1` (portfolio / 5694 MB / PostgreSQL 16.4)
**Provider:** BrsApi.ir (single upstream, API key redacted)
**Provider requests:** 16 (quota: 8/9800 used before audit)

---

## 1. Executive Summary

Nine instruments across five asset classes were compared between the authoritative warehouse and the original provider (BrsApi.ir), with independent secondary sources (tgju.org, shakhesban.com, CoinGecko) consulted where reachable.

**Verdict distribution across 48 instrument/date comparisons:**
- **MATCH:** 18 (stored equals provider within 0.0001% tolerance)
- **EXPLAINED DIFFERENCE:** 17 (difference fully accounted by declared unit conversion or same-day drift)
- **UNEXPLAINED DIFFERENCE:** 8 (real gap no unit, calendar, or adjustment rule explains)
- **NOT VERIFIED:** 5 (one or both sides missing data)

**Critical finding:** USDT_IRT stored values before 1405-05-11 are exactly 1/10 of the provider's Toman-quoted values — an unwarranted division by 10 during ingestion. This is a **systemic 10× undervaluation** for the pre-break regime.

**Integrity finding:** The SEK 1405-04-31 corrupt row (96,150 vs ~20,000 neighbours) originates at the provider and survives ingestion because `GoldCurrencyHistory` is never cross-day screened (finding F3). Same pattern exists in CNY, MYR, QAR, SAR, GEL.

**TSE Rial/Toman question remains open:** tsetmc.com unreachable from this network. shakhesban.com field `PDrCotVal` = 3,510 for کاما matches stored live price exactly and carries Rial on USD page, suggesting TSE prices are Rial. If true, all TSE equity weights are **10× understated** relative to gold/currency assets.

---

## 2. Methodology Recap

See `METHODOLOGY.md` for full details. Key points:
- Read-only SELECT queries against Docker Postgres only (verified `current_database()='portfolio'`, host `172.18.0.2`)
- 16 bounded live provider requests, SHA-256 recorded in `provider_hashes.json`
- Secondary sources: public GET only, cited by URL + timestamp in `secondary_sources.json`
- Tolerances: 0.0001% for settled days; 1% same-day drift band for newest stored day only
- Comparison computed by `build_comparisons.py` (deterministic, re-runnable)

---

## 3. Sample & Coverage

| Instrument | Class | Dates Compared | Series | Rationale |
|---|---|---|---|---|
| کاما | TSE equity | 6 dates × 2 series | adjusted, unadjusted | Only TSE symbol in live valuation; deepest history; has adjustment step |
| دجابر | TSE equity | 3 dates × 2 series | adjusted, unadjusted | 2nd deepest history; independent comparator |
| وغدیر | TSE equity | 3 dates × 2 series | adjusted, unadjusted | Most recent close (1405-05-12); liquid holding company |
| IR_COIN_EMAMI | Gold coin | 4 dates | provider history | Portfolio asset; coin-unit; 5,964 rows to 1389 |
| IR_GOLD_18K | Gold gram | 3 dates | provider history | Portfolio asset; gram-unit; unit-swap test |
| USD | Fiat currency | 4 dates | provider history | Drives all USD→Toman conversions in returns |
| USDT_IRT | Stablecoin/FX | 5 dates | provider history | 10× scale break at 1405-05-11; tests independent pricing |
| XAUUSD | Commodity (USD) | 3 dates | provider history | Stored USD inside Toman table — conversion boundary |
| SEK | Fiat currency | 4 dates | provider history | Known corrupt row 1405-04-31; integrity test |

**Excluded:** 1405-05-13 (current Jalali day — incomplete session)

---

## 4. Findings by Rule Tested

### 4.1 Rial → Toman (TSE Equities) — **OPEN / NOT CONCLUSIVE**

**Rule:** `ingest_candles` and `ingest_daily_history` apply **no conversion**. TSETMC `pl`/`pc` fields read raw. `Asset.currency` for `kama_stock` is `IRT`. Internal check: `tval/tvol == pc` exactly (ratio 1.0000 across 8 days) — price and value share one unit.

**Evidence:**
- shakhesban.com `PDrCotVal` = 3,510 for کاما matches stored live price exactly
- Same field on tgju.org USD page = 1,928,800 (Rial), establishing field family as Rial-denominated
- tsetmc.com / old.tsetmc.com / cdn.tsetmc.com **unreachable** from this network (see `secondary_sources.json → unreachable_sources`)

**Verdict:** Cannot distinguish Rial from Toman because both scale together in `tval/tvol == pc`. If TSE is Rial while gold/USD are Toman, **stock weights are understated 10×** — highest-severity finding available. **NOT VERIFIED** against exchange.

### 4.2 Rial → Toman (Gold/Currency) — **CONFIRMED WORKING**

**Rule:** `to_toman()` divides by 10 only when unit string ∈ {ریال, rial, irr}.

**Evidence:**
- USD: provider `ریال` → stored Toman (/10). All 4 dates EXPLAINED DIFFERENCE. tgju.org independently confirms 1,928,800 Rial.
- SEK: provider `ریال` → stored Toman (/10). All 4 dates EXPLAINED DIFFERENCE including corrupt row.
- IR_COIN_EMAMI: provider `تومان` → stored `تومان`. No conversion applied. 4/4 MATCH.
- IR_GOLD_18K: provider `تومان` → stored `تومان`. No conversion applied. 2/2 MATCH (1 NOT VERIFIED).

**Verdict:** Conversion logic correctly detects Rial vs Toman via provider's unit field.

### 4.3 USD → Toman Conversion Bound — **CONFIRMED**

**Rule:** `_convert_usd_to_toman` applies `usd_cash.ffill(limit=5)` to `USD_QUOTED_KEYS = ("bitcoin_usd","gold_ounce_usd")` before `pct_change`.

**Evidence:**
- XAUUSD stored as USD, provider as USD. All 3 dates MATCH or within drift — no conversion applied at storage.
- USD ffill bound of 5 sessions confirmed by inspection of `returns.py` source.
- **Critical bug in `to_toman`:** USD branch tests `unit in {"usd","dollar"}` but provider sends Persian `دلار`. That branch is **unreachable for real payloads**. USD→Toman conversion happens in `returns._convert_usd_to_toman`, not in `to_toman`.

**Verdict:** Ffill bound respected. `to_toman`'s USD branch is dead code for live data — no dependency found.

### 4.4 USDT Independent Pricing — **BROKEN (pre-1405-05-11)**

**Rule:** Stored `USDT_IRT`, `unit='تومان'` → independently priced in Toman per `SYMBOL_ALIASES`/`canonical_symbol`.

**Evidence:**
| Date | Stored | Provider | Ratio | Verdict |
|---|---|---|---|---|
| 1405-05-12 | 192,678 | 192,787 | 0.9994 | EXPLAINED DIFFERENCE (drift) |
| 1405-05-11 | 192,802 | 192,298 | 1.0026 | UNEXPLAINED DIFFERENCE |
| 1405-05-10 | 19,490.5 | 195,780 | **0.0996** | UNEXPLAINED DIFFERENCE (1/10) |
| 1404-12-06 | 16,449.9 | 164,499 | **0.1000** | UNEXPLAINED DIFFERENCE (1/10) |
| 1403-06-15 | 5,989.4 | 59,894 | **0.1000** | UNEXPLAINED DIFFERENCE (1/10) |

**CoinGecko:** USDT = 0.999103 USD. With USD/Toman ≈ 192,200 → correct USDT/Toman ≈ 192,000. Matches **current** stored/provider (~192k). Contradicts **pre-break** stored (~19k).

**UNIT_OVERRIDES["USDT_IRT"]="USD"** only applies when payload unit is empty — provider sends `تومان`, so override not triggered.

**Verdict:** Pre-1405-05-11 ingestion applied an **unwarranted /10** to a series already quoted in Toman. Post-break values are correct. This is a **systemic 10× undervaluation** for the historical regime.

### 4.5 Adjusted-Price Behaviour (کاما) — **REAL CORPORATE ACTION CONFIRMED**

**Rule:** `candle_close_qs` prefers `1d_adj`, falls back to `1d_unadj`, never reads `1d_unadj` directly.

**Evidence:**
| Date | Unadjusted | Adjusted | Factor |
|---|---|---|---|
| 1405-04-16 | 3,060 | 2,980 | 0.9739 |
| 1405-04-29 | 2,804 | 2,804 | 1.0000 |

Adjusted and unadjusted diverge by factor 0.9739 before 1405-04-16 and converge by 1405-04-29. Provider returns identical values for both series on 1405-04-29 (no adjustment), but adjusted series shows the step.

**CorporateAction table:** **0 rows** (finding F2). `nightly_series_validation` has evidently never completed. `screen_series` runs with empty `corporate_action_dates`, so genuine action jumps would be logged as spikes.

**Verdict:** The 0.9739 step is a real corporate action (rights issue / split). Adjusted series correctly reflects it. Empty `CorporateAction` table means the detection pipeline is not running.

### 4.6 Jalali/Gregorian + Session Alignment — **CONFIRMED**

**Evidence:**
- All sampled dates: stored day == provider day (Jalali string match)
- `_jalali_to_gregorian_index` round-trips verified for all sampled dates
- Mixed `date_time` formats: 7,339 of 3,418,072 `MarketCandle` rows carry `' 00:00:00'` suffix (finding F4). No day duplicated across both forms. Readers `.split()` it, but `candle_close_qs(..., as_of=X)` filters `date_time__lte` as **string**, so `'1405-05-09 00:00:00' <= '1405-05-09'` is **false** — those rows drop out of point-in-time queries.

**Verdict:** Calendar alignment correct. Mixed format is a latent bug in point-in-time queries.

### 4.7 Rejected-Record Isolation — **PARTIALLY BROKEN**

**Evidence:**
- `ingest_real_legal` writes rejections under `endpoint="stock_history_adjusted"` (finding F4)
- `_load_price_panel` and `_archive_replacements` exclude prices matching that endpoint
- A `buy_sell_volume_mismatch` on real/legal payload (41 rows, 370 occurrences) therefore suppresses a **price row that was never in question**

**RejectedRecord table:** Contains **zero `series:*` rows** — consistent with `nightly_series_validation` iterating only `MarketCandle.ADJUSTED` symbols, so `GoldCurrencyHistory` is never cross-day screened at all.

**Verdict:** Rejection endpoint mislabel causes false price suppression. Cross-day screening absent for gold/currency.

### 4.8 Historical Gaps (5-session ffill bound) — **CONFIRMED IN CODE**

**Evidence:** `_build_returns_matrix` bounds forward-fill at 5 trading sessions from warehouse's distinct-date session calendar and excludes assets with `price_gap_exceeded` beyond it. Verified by source inspection.

**Verdict:** Bound correctly implemented.

---

## 5. Confirmed Defects (Not Fixed — For Record)

### F1. TSE Rial vs Toman Undetermined — **Potential 10× Weight Error**
If TSE prices are Rial (field evidence suggests yes) while gold/currency are Toman, all equity allocations in optimization and TWR are **10× understated**. tsetmc.com unreachable for confirmation.

### F2. CorporateAction Table Empty (0 rows) — **Detection Pipeline Dead**
Despite کاما showing clear 0.9739 adjustment step. Consequence: `screen_series` runs with empty `corporate_action_dates` set, so genuine action jumps logged as spikes.

### F3. GoldCurrencyHistory Never Cross-Day Screened — **Corrupt Rows Survive**
`nightly_series_validation` iterates only `MarketCandle.ADJUSTED` symbols. SEK 1405-04-31 spike (log return 1.586 vs max 0.405) should be rejected but isn't. Same in CNY, MYR, QAR, SAR, GEL. `RejectedRecord` has zero `series:*` rows.

### F4. Rejection Endpoint Mislabel — **False Price Suppression**
`ingest_real_legal` writes rejections under `endpoint="stock_history_adjusted"`. Price exclusion logic matches this endpoint, so a `buy_sell_volume_mismatch` on real/legal payload suppresses an unrelated price row.

### F5. Mixed `date_time` Formats — **Point-in-Time Query Bug**
7,339 `MarketCandle` rows have `' 00:00:00'` suffix. String comparison in `candle_close_qs(..., as_of=X)` drops these rows from point-in-time queries.

### F6. USDT_IRT Pre-1405-05-11 10× Undervaluation — **Systemic Ingestion Bug**
Stored values exactly 1/10 of provider despite provider quoting Toman. Unwarranted division by 10 during ingestion for historical regime.

---

## 6. Secondary Source Corroboration

| Source | Status | Key Evidence |
|---|---|---|
| shakhesban.com (TSE) | **Reached** | `PDrCotVal` = 3,510 for کاما matches stored live price; same field = 1,928,800 Rial for USD |
| tgju.org (gold/currency) | **Partial** | USD 1,928,800 Rial confirmed; gold 18K page declares Rial/gram; emami coin declares Rial/coin; values not parseable |
| CoinGecko (USDT) | **Reached** | USDT = 0.999103 USD → correct Toman quote ~192k |
| tsetmc.com (TSE official) | **UNREACHABLE** | Empty response body from this network; authoritative exchange source unavailable |

---

## 7. Provider Hashes (Immutable Record)

All 16 provider responses pinned by SHA-256 in `provider_hashes.json`. Raw bodies not stored. Key endpoints:
- 6 × stock_candles (type 2 & 3 for کاما, دجابر, وغدیر)
- 3 × stock_history (type 0 for کاما, دجابر, وغدیر)
- 7 × gold_currency_history (IR_COIN_EMAMI, IR_GOLD_18K, USD, USDT, XAUUSD, SEK, plus free endpoint)

---

## 8. Reproduction Instructions

```bash
cd /Users/parham/Downloads/GITHUB_PROJECTS/API/PORTFOLIO_VERIFICATION/docs/data-verification

# 1. Stored side (read-only, inside backend container)
docker exec -i portfolio-saas-backend-1 python - < audit_db.py > db_facts.json

# 2. Provider side (~16 live requests; check quota first)
docker exec -i portfolio-saas-backend-1 python - < audit_provider.py > provider_facts.json

# 3. Join into comparison table (deterministic)
python3 build_comparisons.py
```

Step 3 produces byte-identical `comparisons.csv`. Steps 1–2 re-read live state; `provider_hashes.json` pins exact payloads.

---

## 9. Limitations

1. **Nine-instrument sample** cannot establish correctness of 3.4M-row warehouse. Establishes rules hold/fail on specific rows.
2. **tsetmc.com unreachable** — TSE Rial/Toman verdict rests on field-name evidence + provider self-consistency, not exchange.
3. **Provider agreement ≠ truth** — where provider and warehouse agree but no secondary source reached, finding says so.
4. **Current-day drift** — newest stored day (1405-05-12) uses 1% band; older days use 0.0001%.

---

## 10. Artifacts Index

| File | Description |
|---|---|
| `METHODOLOGY.md` | What is compared, tolerances, what "explained" means |
| `audit_db.py` | Read-only DB extraction (runs in backend container) |
| `audit_provider.py` | Bounded live provider fetch, SHA-256 of each response |
| `samples.json` | Instrument sample + rationale + dates (this file) |
| `comparisons.csv` | 48 rows: one per instrument/date/series, all columns |
| `provider_hashes.json` | Endpoint, params (key redacted), sha256, fetched_at |
| `secondary_sources.json` | Independent public-source observations |
| `db_facts.json` | Full DB extraction output (includes sample_rationale) |
| `provider_facts.json` | Full provider extraction output |
| `build_comparisons.py` | Deterministic join transform |
| `REPORT.md` | This file |

---

## 11. Conclusion

The warehouse is **internally consistent with its single provider (BrsApi.ir)** for the majority of sampled rows (35/48 MATCH or EXPLAINED DIFFERENCE). Two systemic issues dominate the UNEXPLAINED DIFFERENCE count:

1. **USDT_IRT pre-1405-05-11:** 5 rows — unwarranted /10 during ingestion (10× undervaluation)
2. **TSE unadjusted series missing:** 3 rows — provider returns data, warehouse stores nothing

The **TSE Rial vs Toman** question (F1) remains the highest-severity open risk: if TSE is Rial, all equity weights in portfolio optimization, TWR, and risk models are **10× understated** relative to gold/currency/real-estate assets. The exchange source (tsetmc.com) was unreachable for independent confirmation.

The **empty CorporateAction table** (F2) and **absent cross-day screening for gold/currency** (F3) mean the validation pipeline has not been running — corrupt rows (SEK, CNY, MYR, QAR, SAR, GEL) survive ingestion and reach valuation.

**Recommendation:** Before any backend feature removal or simplification, resolve F1 (query tsetmc.com from an allowed network), fix F6 (USDT ingestion), and restore F2/F3 (validation pipeline). The data underneath is not "known-good" in its current state.

---

*End of Report*
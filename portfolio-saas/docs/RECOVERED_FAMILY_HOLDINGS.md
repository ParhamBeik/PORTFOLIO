# Recovered Father / Mother holdings

**Recovered:** 2026-08-09
**Sources (cross-checked):**
1. Postgres dump `PORTFOLIO_BACKUPS/portfolio-20260807-210843.dump` — `portfolio_account` Mother/Father + `portfolio_holding` (imported 2026-08-03)
2. `archive/portfolio-new-structure/template/Portfolio_Tracker_v2.xlsx` — Dashboard/Allocation/Live_Data (2026-07-18 prices)
3. Note: `current_state.json` was always gitignored, so git history never held quantities

Swiss bars + pre-86 quarters were **not** in the SaaS asset catalog at import time, so they are missing from the dump; they are the unique integer solution that makes Mother gold match the Excel Allocation total exactly at Live_Data prices.

Valuation check at Live_Data prices (Emami 188,510,000 … KAMA 2,807, USD 193,400): Mother **12,172,999,210** / Father **11,814,530,000** — matches Dashboard.

## `current_state.json` shape

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

House constants (from archived settings): `house_area_sqm=90.2`, `house_mortgage_deduction=400_000_000`.

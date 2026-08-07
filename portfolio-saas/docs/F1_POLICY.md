# F1 Policy — TSE price unit (active)

**Status:** `TSE_PRICE_UNIT = unverified`  
**Effective:** 2026-08-04  
**Code constant:** `marketdata.currency.TSE_PRICE_UNIT`

## Rules

1. Do **not** divide or multiply historical/live TSE prices by 10 without authoritative evidence.
2. While `unverified`, optimization and efficient-frontier paths that mix TSE symbols with non-TSE (gold/FX/crypto/commodity) series **must fail closed** with an explicit error — no silent cross-unit weights.
3. TSE-only universes may still optimize (relative TSE weights share one unit).
4. Non-TSE-only universes are unchanged.
5. Valuation continues to return numbers but marks TSE holdings with `price_unit_status=unverified` and sets account-level `tse_unit_policy`.
6. Flip `TSE_PRICE_UNIT` to `rial` or `toman` only after documented evidence; then update tests and, if `rial`, run a manifest-backed backfill.

## Rollback

Set `TSE_PRICE_UNIT` back or remove the guard only when unit is verified and tests updated.

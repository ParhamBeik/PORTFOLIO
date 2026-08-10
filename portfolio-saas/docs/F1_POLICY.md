# F1 policy — TSE price units

**Status:** provider and warehouse unit verified as Rial
**Verified:** 2026-08-06
**Code constant:** `marketdata.currency.TSE_PRICE_UNIT = "rial"`

## Active boundaries

1. BrsApi TSE history, candles, and transaction ticks are stored provider-verbatim in Rial.
2. Analytics panels that combine TSE data with Toman assets convert through `tse_close_to_toman()`.
3. Portfolio TSE prices currently stay Rial because recovered stock quantities use one tenth of broker share counts; numerically, `quantity × Rial price` matches the intended Toman value.
4. Gold, FX, manual assets, snapshots, liabilities, and aggregate totals remain Toman.

## Known ceiling

The one-tenth-share convention is legacy-data compatibility, not a general multi-user unit model. Before accepting ordinary broker share counts, migrate existing TSE holdings and ledger quantities by 10, restore TSE `portfolio_price` to Toman, and update the boundary tests together.

## Fail-safe

If `TSE_PRICE_UNIT` returns to an unverified value, mixed TSE/non-TSE optimization must fail closed through `MixedUnitUniverseBlocked`.

# MVP data preflight — 2026-09-22

Read-only queries against the live PostgreSQL database on `45.139.10.12`; no rows were changed. This is **not** the required before/after migration audit.

| Measure | Live result |
|---|---:|
| Optimization snapshots | 3,562 rows, 12 `(account, scenario, basis, window)` keys |
| Net-worth snapshots | 60,005 rows, 555 `(user, account, Tehran day)` keys |
| Fractional holding quantities | 0 rows |
| Fractional ledger quantities | 0 rows |
| Fractional ledger unit prices | 0 rows |
| Fractional portfolio prices | 0 rows |
| Fractional liability amounts | 0 rows |
| Fractional ledger amounts | 5 rows; aggregate `ROUND_HALF_UP` delta 0 Toman |
| Fractional snapshot values | 40 rows; aggregate `ROUND_HALF_UP` delta +0.2131 Toman |

The fractional counts do not prove that all assets should be indivisible: crypto, USDT, and gram gold must continue to accept fractional holdings even if the present sample happens to contain only whole quantities. Property `quantity` is currently a price-per-square-metre-in-millions, not a unit count, and needs a separate migration.

Release gate: restore a production backup to staging; capture every monetary or quantity row whose stored value would change; record old value, `ROUND_HALF_UP` target, unit, source table/key, and aggregate valuation delta; then rehearse migrations and verify one-day snapshot priority before production deployment.

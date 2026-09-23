# MVP staging migration rehearsal — 2026-09-23

Source artifact: production encrypted backup `daily-2026-09-23.dump.enc`,
SHA-256 `0a792d2d8f69f18e936bfc8133f95ea1b210021b28d2fb040ed082f6d232bc3b`.
The checksum verified on the VPS. The backup was decrypted over SSH into an
isolated local TimescaleDB 2.17.2 / PostgreSQL 16 staging container; no
production database or application code was changed. The container's database
port is bound to `127.0.0.1:55433` only.

The first local PostgreSQL 18 restore was stopped because TimescaleDB was not
installed there. The clean restore to the matching TimescaleDB image completed
without errors, followed by `timescaledb_post_restore()`.

A temporary ordinary test user with one gold holding was added only to the
staging copy after migration/audit checks. Authenticated browser tests covered
the four destinations and ordinary-user Operations denial. The local Django
test server was shut down afterward; the TimescaleDB staging container was
stopped with its data volume preserved for further rehearsal. It was restarted
locally to rehearse the atomic-quantity shadow migration; production remains
untouched.

| Check | Before | After | Result |
|---|---:|---:|---|
| Users / accounts / holdings / ledger | 2 / 2 / 15 / 134 | 2 / 2 / 15 / 134 | unchanged |
| Core-row checksum | `0c5c8a51244a7c555529050308c39a2668c866e38a062e2375a3aa3b04ad62f8` | same | pass |
| Optimization snapshots | 3,578 rows / 12 current keys | 12 rows / 12 keys | 3,566 historical rows intentionally removed |
| Net-worth snapshots | 58,949 | 558 rows / 558 distinct Tehran-day scope keys | 58,391 intraday or synthetic duplicate rows removed; oldest 2026-02-04 |
| Admin/user role invariant | 1 legacy privileged, 1 ordinary | 1 `admin` with both Django flags, 1 `user` with neither | pass |
| `Asset.currency` column | present (15 IRT, 1 USD) | absent | pass; instrument identity retained |
| Legacy quota rows | 30 `brs`, 59 `tsetmc` | same | retained as historical evidence |
| Whole-Toman storage audit | 5 fractional ledger amounts, 43 fractional retained daily snapshots | 48 permanent `MonetaryRoundingAudit` rows | ledger net delta 0 Toman; retained snapshot net delta +0.0123 Toman |
| Atomic quantity / property price shadow | 16 holdings, 134 quantity-bearing ledger rows | 16 and 134 exactly reconstructed from new columns | no quantity rounding or financial delta |

Migrations applied: `accounts.0003_user_role`, `marketdata.0010_quota_evidence`,
`portfolio.0038_current_optimization_snapshots`,
`portfolio.0039_remove_asset_currency`, and
`portfolio.0040_daily_snapshot`, `portfolio.0041_whole_toman_audit`, and
`portfolio.0042_atomic_quantity_shadow`. Django system check and
`migrate --check` passed afterward. The later
`marketdata.0011_provider_meter_observation` also applied cleanly to this
staging copy, and Django system check passed again.

The whole-money migration ran `ROUND_HALF_UP` before reducing the database
scale of eight unambiguous Toman fields. Its permanent audit stores source
table, row ID, field, before value, after value, and delta for each changed
row. It does not yet convert the mixed-unit provider `Price.price` field,
the mixed-unit unit-price fields. Migration 0042 adds integer atomic units and
whole-Toman property price per square metre, validates that indivisible rows
have no fractions, and backfills them without changing existing values. These
are dual-written shadow columns until application reads and constraints can
be switched over; the old decimal `quantity` column still exists.

The daily migration kept 186 rows per scope (aggregate and two accounts).
Among aggregate rows, 19 are verified session closes and 155 are legacy
estimated observations; those flags remain visible for provenance. The
deterministic priority was verified close, latest real observation, then
latest estimated observation, with timestamp and ID tie-breakers. New daily
rows are written once at 00:01 Tehran for the prior day; current-day value is
derived at read time. A final fresh restore and rehearsal is still required
after the financial-storage migration is written.

This rehearses only the migrations currently implemented. It does **not**
authorize production deployment yet: the atomic-quantity read-path cutover,
remaining monetary boundaries, and fresh staging rehearsal are required before
release.

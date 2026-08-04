PR: Add price_unit metadata and safe backfill

Summary

This PR adds conservative metadata to price rows to capture provider-declared units and verification status, plus ingestion defaults that avoid silent mixed-unit valuation. It includes:

- Price model fields: price_unit (IRT/IRR/UNKNOWN), price_unit_verified (bool)
- Ingestion defaults: new writes mark TSE-derived prices UNKNOWN/unverified; BRS/manual marked IRT/verified
- Migration 0020: adds fields non-destructively
- Migration 0021: batched SQL backfill (sets UNKNOWN for TSE-linked, IRT for BRS-only)
- Operator staging script: .github/scripts/run_staging_migration_check.sh (also copied to repo scripts/)
- GitHub Actions workflow to run focused backend tests for PRs touching portfolio app

Why

There is ambiguity about TSETMC price units (Rial vs Toman). These changes avoid silent unit conversion and make unit state explicit for operational review.

Rollout plan

1. Push branch and open PR.
2. Run CI; review failing tests.
3. Run corrected staging script to restore latest logical backup into isolated DB and validate migrations.
4. Deploy migration 0020 to staging, deploy code, run batched backfill (0021) during maintenance window, run acceptance tests.
5. After operator approval, schedule production rollout with full backups and restore-proof.

Testing & Validation

- CI focuses on financial-safety, valuation, and optimization tests.
- Staging script validates database schema, sample asset rows, numeric parity, and admin/API exposure.

Rollback

- 0021 reverse_sql resets metadata to UNKNOWN/false; numeric values are untouched.
- Production rollback plan: restore from pre-migration backup if unexpected numeric effects observed.

Notes

- 0021 uses guarded UPDATE to avoid overwriting TSE-marked rows when assets have both tse_symbol and brs_symbol.
- For very large DBs, prefer running backfill in controlled batches rather than a single-transaction migration.

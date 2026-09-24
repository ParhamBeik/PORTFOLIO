# Portfolio MVP implementation checklist

Status on 2026-09-24. Checked items are implemented and tested; deployment status is tracked separately below.

## Quota

- [x] Split provider products (`aio`, `market_cgcc`) from internal workload buckets.
- [x] Record local attempts, successes, provider-observed usage, and variance separately.
- [x] Classify `Gold_Currency_Pro` using endpoint parameters and test the rule locally.
- [x] Run an isolated <10-request provider counter matrix for TSETMC, Codal, Market live, and Pro default/history modes; save before/after evidence in `BRSAPI-QUOTA-EVIDENCE-2026-09-22.md`. This ran after, not at, the Tehran reset; repeat at reset if that timing is a strict release criterion.
- [x] Verify every registered provider path from the VPS against the paused-worker BrsApi panel; 13 successful paths and two billed HTTP 400 cases are documented in the same evidence report.
- [x] Implement and test provider-counter observation and rising-variance alert.
  - [x] Verify the VPS account-panel read and prove two stopped-worker reads do not move either meter; stage the opt-in flag in the VPS-only environment for deployment.
  - [x] Separate the pre-observation provider baseline from subsequent local attribution, so activating the new product rows mid-day does not report all earlier provider usage as variance.
  - [x] Keep quota live within the cached Operations overview; display a negative signed variance as an observation-time race, not a billing alert.

## Authorization and administration

- [x] Add `admin`/`user` role, migrate legacy privileged users, and enforce matching Django flags.
- [x] Remove staff/superuser flags from user-facing serializers and admin forms.
- [x] Keep all application, Group, and JWT blacklist models discoverable in Django admin; security-sensitive tokens and generated warehouse records are read-only.
- [x] Add a visual admin-only Operations inventory linking every registered admin model, alongside existing monitoring and quotas.
- [x] Complete the authorization regression suite, including role changes and refresh-token revocation.

## Financial semantics and storage

- [x] Expose quote unit, valuation unit, exposure group, and quantity scale in asset API responses; physical USD and USDT remain separate instruments.
- [x] Remove the legacy `Asset.currency` database field after converting its remaining consumers.
- [x] Split live, daily-average, and ledger execution prices into zero-decimal Iranian and precision-preserving foreign fields; retain fractional property acquisition quotes expressed in millions of Toman.
- [x] Store divisible quantities as integer atomic units and property price-per-square-metre as whole Toman; remove legacy decimal quantity columns after exact verification.
- [x] Generate and verify the permanent row-level rounding audit and aggregate financial delta.
  - [x] Eight unambiguous Toman fields converted and rehearsed; 48 changed rows in a permanent audit, net ledger delta 0 and retained-snapshot delta +0.0123 Toman. Mixed-unit prices and quantities remain.
  - [x] Rehearse exact integer-atomic quantity and whole-Toman property-price cutover. All 16 staging holdings and 134 quantity-bearing ledger rows reconstruct exactly and have permanent before/after audit rows; the legacy `quantity` columns are removed.
  - [x] Rehearse mixed-unit price cutover: 77,555 Iranian live quotes and 398 daily averages converted; 284 fractional daily averages audited and rounded, aggregate delta −0.4823 Toman. USD-native quotes retain precision.

## History, guidance, and product

- [x] Provide four user destinations and admin-only Operations, with legacy route redirects.
- [x] Keep Snapshot and OptimizationSnapshot visible as read-only admin diagnostics while Portfolio history and Guidance remain their user-facing consumers.
- [x] Deduplicate historical net-worth snapshots to one Tehran-day row per scope in matching TimescaleDB staging (58,949→558), schedule one daily write, and derive today without persistence.
- [x] Replace append-only optimization history with atomic current-row upserts and deterministic deduplication migration; staging reduced 3,578 historical rows to 12 current keys.
- [x] Provide consolidated `/api/guidance/` with Balanced default, 3Y/1Y fallback disclosure, and compact benchmark.
- [ ] Retire duplicate optimization endpoints only after the compatibility release.
  - [x] Authenticated staging E2E covers all four destinations, Guidance, Operations denial, redirects, Markets history, Activity import preview, and Portfolio panels (23 passed, 3 skipped for sparse test holdings). Obsolete page-specific optimization E2E checks were replaced with Guidance coverage.

## Release gate

- [x] Prevent a mid-day product-key rename from creating fresh zero-usage wallets: when the panel meter is enabled, admission waits for the first complete provider observation. Verify this with a database regression test. Production meter is enabled.
- [x] Verify the 2026-09-23 production backup checksum and restore it into matching isolated TimescaleDB staging; rehearse implemented migrations through `portfolio.0042` (`MVP-STAGING-REHEARSAL-2026-09-23.md`).
- [x] Rehearse quantity, eight whole-Toman fields, and mixed-unit price conversion against isolated TimescaleDB staging with permanent row-level audits.
- [x] Run complete backend/frontend tests and local smoke checks.
  - [x] Backend 1,194 passed / 1 skipped; migration autodetector reports no changes on a clean database; frontend 26 unit and 50 component tests, lint, build; staged browser E2E 23 passed / 3 skipped, plus one heavy comparison matrix not run.
- [x] Deploy commit `b8a68b8` manually through the normal VPS deployment script after CI test gates passed; migrations, backup, service health, public routes, role consistency, and financial audit rows verified.
- [x] Deploy the cold-request timeout and quota-baseline follow-up; verify the Operations display and post-baseline reconciliation on production (`6456ef8`).
- [ ] Deploy and verify the live-quota cache follow-up.
- [ ] Repair GitHub-hosted runner SSH access; CI test gates pass, but its deploy job times out before reaching the VPS. Local Mac SSH and manual deployment work.
- [ ] Verify a complete next Tehran-day quota cycle with provider totals and local attribution. Preserve any genuine unattributed variance instead of forcing equality.

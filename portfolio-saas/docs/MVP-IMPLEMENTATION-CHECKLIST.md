# Portfolio MVP implementation checklist

Status on 2026-09-23. A checked item means implemented and verified locally, not deployed.

## Quota

- [x] Split provider products (`aio`, `market_cgcc`) from internal workload buckets.
- [x] Record local attempts, successes, provider-observed usage, and variance separately.
- [x] Classify `Gold_Currency_Pro` using endpoint parameters and test the rule locally.
- [x] Run an isolated <10-request provider counter matrix for TSETMC, Codal, Market live, and Pro default/history modes; save before/after evidence in `BRSAPI-QUOTA-EVIDENCE-2026-09-22.md`. This ran after, not at, the Tehran reset; repeat at reset if that timing is a strict release criterion.
- [x] Verify every registered provider path from the VPS against the paused-worker BrsApi panel; 13 successful paths and two billed HTTP 400 cases are documented in the same evidence report.
- [ ] Automate reliable provider-counter observation and alert on rising product-level variance.
  - [x] Implement and test an opt-in read-only panel reader, observation timestamps, and rising-variance alert; activation still needs a verified VPS account-panel credentialed read.

## Authorization and administration

- [x] Add `admin`/`user` role, migrate legacy privileged users, and enforce matching Django flags.
- [x] Remove staff/superuser flags from user-facing serializers and admin forms.
- [x] Keep all application, Group, and JWT blacklist models discoverable in Django admin; security-sensitive tokens and generated warehouse records are read-only.
- [x] Add a visual admin-only Operations inventory linking every registered admin model, alongside existing monitoring and quotas.
- [x] Complete the authorization regression suite, including role changes and refresh-token revocation.

## Financial semantics and storage

- [x] Expose quote unit, valuation unit, exposure group, and quantity scale in asset API responses; physical USD and USDT remain separate instruments.
- [x] Remove the legacy `Asset.currency` database field after converting its remaining consumers.
- [ ] Convert Iranian money to zero-decimal storage, and divisible quantities to integer atomic units.
- [ ] Move property price-per-square-metre out of `quantity`.
- [ ] Generate and verify the permanent row-level rounding audit and aggregate financial delta.
  - [x] Eight unambiguous Toman fields converted and rehearsed; 48 changed rows in a permanent audit, net ledger delta 0 and retained-snapshot delta +0.0123 Toman. Mixed-unit prices and quantities remain.
  - [x] Add and rehearse exact integer-atomic quantity and whole-Toman property-price shadow columns. All 16 staging holdings and 134 quantity-bearing ledger rows reconcile; legacy `quantity` remains the read column until the application cutover.

## History, guidance, and product

- [x] Provide four user destinations and admin-only Operations, with legacy route redirects.
- [x] Keep Snapshot and OptimizationSnapshot visible as read-only admin diagnostics while Portfolio history and Guidance remain their user-facing consumers.
- [x] Deduplicate historical net-worth snapshots to one Tehran-day row per scope in matching TimescaleDB staging (58,949→558), schedule one daily write, and derive today without persistence.
- [x] Replace append-only optimization history with atomic current-row upserts and deterministic deduplication migration; staging reduced 3,578 historical rows to 12 current keys.
- [x] Provide consolidated `/api/guidance/` with Balanced default, 3Y/1Y fallback disclosure, and compact benchmark.
- [ ] Retire duplicate optimization endpoints only after the compatibility release.
  - [x] Authenticated staging E2E covers all four destinations, Guidance, Operations denial, redirects, Markets history, Activity import preview, and Portfolio panels (23 passed, 3 skipped for sparse test holdings). Obsolete page-specific optimization E2E checks were replaced with Guidance coverage.

## Release gate

- [ ] Prevent a mid-day product-key rename from creating fresh zero-usage wallets while the provider already has billed requests; deploy only with a verified provider-counter bootstrap or a controlled reset-window cutover.
- [x] Verify the 2026-09-23 production backup checksum and restore it into matching isolated TimescaleDB staging; rehearse implemented migrations through `portfolio.0042` (`MVP-STAGING-REHEARSAL-2026-09-23.md`).
- [ ] Rehearse and audit all financial migrations against a staging copy.
- [ ] Run complete backend/frontend tests and smoke checks.
  - [x] Backend 1,189 passed / 1 skipped after admin and meter changes, plus the final added meter-task test passed separately; frontend 26 unit and 50 component tests, lint, build; staged browser E2E 23 passed / 3 skipped, plus one heavy comparison matrix not run.
- [ ] Deploy, then reconcile provider counters and verify role/financial invariants.

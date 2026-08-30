# portfolio-saas

Real-time portfolio tracker for the Iranian market (BRS gold/currency/crypto + TSETMC stocks).
Multi-user from day one: prices are fetched once globally and shared across every user (see README "Why it scales").

**Stack:** React (JS, Vite, Tailwind 4, echarts) + Django REST + PostgreSQL/TimescaleDB + Redis + Celery, Docker Compose.
Full API surface, layout tree, and prod deploy steps: [`README.md`](README.md). Price unit policy: [`docs/REFERENCE.md`](docs/REFERENCE.md).

## Layout

- `backend/config/` — Django project: settings, urls, celery, health, `api.py` (JSON rendering + global error translation), `observability.py` (request id, log filter, Sentry, operator alerts)
- `backend/accounts/` — User model + JWT auth
- `backend/portfolio/` — user-portfolio domain: models (Asset, Account, Holding, Price, Snapshot, LedgerEntry), `services/` (valuation, ledger, performance, returns, diagnostics, optimization), `live/` (price loop: fetcher, extractor, redis_client), `tasks.py` (Celery heartbeat)
- `backend/marketdata/` — market-history warehouse, separate bounded context: symbol-keyed tables, no user FKs. `fetchers.py` (every BrsApi client, validated against the `endpoints.py` registry), `ingest.py`, `archive.py`/`quota.py` (gap-driven backfill under a request budget), `calendars.py` (which days a market was open), `admin_api.py` (staff-only Ops console plus its urlpatterns)
- `backend/tests/` — 11 thematic suites, one per bounded concern; each merges the older single-topic files and keeps their banners
- `frontend/src/` — `pages/` (Dashboard, Ops, MyOptimal, BestOverall, Family, Ledger, Onboarding), `components/ui.jsx` (shared primitives: Card, StatTile, Async, Table, Button…), `components/charts.jsx` (themed echarts wrappers), `api.js` + `useApi.js` (the one fetch pattern), `format.js` (number/date formatting)

Each app has one squashed schema migration (`0001_squashed`, carrying `replaces=`);
marketdata adds `0002_storage_tuning` for autovacuum triggers and the TimescaleDB
tick hypertable. Do not resurrect the one-shot data repairs they replaced — those
have run on every deployed database and a fresh one has nothing to repair.

## Commands

```bash
# full stack (dev)
docker compose up --build          # frontend :5173, API :8000

# backend only (needs local Postgres + Redis)
cd backend && pip install -r requirements.txt -r requirements-dev.txt
python manage.py migrate && python manage.py seed_assets && python manage.py seed_demo
python manage.py runserver
celery -A config worker -l info & celery -A config beat -l info &

# backend tests (local Postgres required — DISTINCT ON; sqlite cannot run them)
python -m pytest -q

# frontend
cd frontend && npm install && npm run dev      # proxies /api to :8000
npm run build
npx playwright test                            # e2e, needs E2E_EMAIL/E2E_PASSWORD env
```

Demo users (DEBUG only): `demopro@portfolio.local` / `demopro12345`.

## Domain invariants (migrated from global learned-patterns — PORTFOLIO-specific, keep here not in `~/.claude/CLAUDE.md`)

- Adjusted prices are read only from `MarketCandle(timeframe="1d_adj")`, never from `DailyStockHistory.is_adjusted=True` (that model now serves unadjusted reads only). Any table feeding the returns matrix must be added to `_price_version_fingerprint()` or cached returns survive new ingests.
- In the archive verifier, the read-back in `_fetch_and_ingest` must key on the symbol the ingest path actually wrote, not `state.symbol` — providers canonicalize on write (gold `USDT`→`USDT_IRT`, codal `سامان2`→`سامان`), and a mismatch yields stored=0 against a non-empty expected set: a state that never converges and re-fetches every cycle forever.
- Every price-resolution path — the returns matrix, `value_as_of` (feeds TWR cash-flow boundaries) AND `compute_dynamic_net_worth_series` (feeds the chart) — must bound forward-fill at 5 trading sessions via the one helper `marketdata.calendars.sessions_between()`, and exclude the asset with `price_gap_exceeded` beyond it. A bare `date__lte(...).order_by("-date").first()` silently values today's holding at a delisted asset's last close. Two ways to get the bound wrong, both of which shipped at once: counting **calendar days** (a closure or the Thu/Fri weekend ages a price faster than the market does, so the guard fires early and drops a live holding — this produced a fake two-day portfolio cliff on 2026-07-06/07), and measuring against **one symbol's own rows** (that symbol's latest row is by construction the newest at-or-before `as_of`, so the count is always 0 and the guard can never fire). The calendar must be market-wide and measured in sessions. **Known exception:** `calendars.market_for_asset` has only two calendars, so crypto (which prints 7 days a week) is aged against `gold_currency`, whose Thu/Fri closure makes 5 sessions ≈ 7+ calendar days. The bound is therefore looser for crypto than it reads; it errs toward keeping a holding, never toward dropping a live one.
- `SnapshotListView`'s synthetic fallback must trigger only when there is genuinely nothing recorded. Comparing observed snapshot days against *calendar* days in the window is permanently true — markets are shut Thursday and Friday — so it discarded real history on every request for every holdings-only account.
- Before deploying a squashed migration, diff its `replaces` list against the target's `django_migrations` and confirm the generated file defines `Migration` exactly once. A partially-applied squash is silently discarded in favour of migrations that no longer exist on disk, and a duplicated class definition drops `replaces` entirely — both fail the deploy at `CreateModel`, not at `migrate --check`.
- The F1 TSE price-unit guard belongs inside `daily_returns_matrix()` (`portfolio/services/returns.py`), not in `optimization.py` — all ~8 mixed-unit consumers (diagnostics/AnalyticsView, AssetReturnsView, market catalog, `nightly_asset_metrics`, warmup_cache) route through that one function; guarding only the optimizer leaves the rest computing/caching/persisting cross-unit numbers that may be 10× wrong.
- Commodity/crypto/option history is sourced from live-fetch organic ingest in `portfolio.tasks`, retired from the archive backfill path (`_RETIRED_ARCHIVE_ENDPOINTS` in `marketdata/archive.py`). Never re-add them to `_FULL_HISTORY`/`_ENDPOINT_PRIORITY` or they fall through to the gold fetch branch.
- Monetary storage standardization on Rial was tried (2026-08-05) and **reverted** (2026-08-06): `marketdata/ingest.py` writes every BrsAPI field verbatim with zero multipliers (the warehouse is a faithful copy of the provider payload, unit label included). `marketdata/currency.py:to_toman()` is the only conversion helper, used solely by `portfolio/live/extractor.py` to blend sources into the Toman-denominated `portfolio_price`. Never call it from `ingest.py`; never reintroduce a blanket `x10`/manifest-multiply on warehouse tables.
- All asset-eligibility gating for the returns matrix (`price_gap_exceeded`, `insufficient_history`, `insufficient_coverage` at the 0.90 bar) lives in `_build_returns_matrix()` (`portfolio/services/returns.py`). `optimize()` should only add its own `min_observations`/zero-variance filter before `dropna(how="any")` — a coverage filter there is dead code shadowing the upstream reason codes the UI surfaces.
- When multiple call sites independently re-implement the same validation/threshold check with different magic numbers (e.g. duplicated "bad price" spike-detection heuristics), consolidate into one shared module/constant rather than patching each call site.
- **Provider quota is per API key, never global.** BrsApi meters each subscription separately: `Tsetmc/*` + `Codal/*` against `TSETMC_API_KEY` (~10,000/day), `Market/*` against `BRS_API_KEY` (~1,500/day). `ApiRequestQuota` is keyed `(day, plan)` and `endpoints.Endpoint.plan` is the one place the mapping is declared. There is deliberately **no** `MARKETDATA_DAILY_REQUEST_LIMIT`: `bucket_budget(ARCHIVE)` returns `None` (uncapped as a bucket). `MARKETDATA_PLAN_LIMIT_TSETMC` / `_BRS` are per-plan *expectations* used only to size the live reserve until the provider discloses `row.limit` (which is 0 on most days because it only arrives on error responses). Without them archive spent a whole TSETMC wallet before dawn on 2026-08-26 with `live_used=0`. Live is static; leftover is paced across the Tehran day (`archive_allowance_now`). An archive-triggered breaker must not silence live. `limit=0` on a row means "not yet disclosed" — never render it as "0 remaining". A single shared counter is what let a full TSETMC backfill refuse gold/currency calls with 79% of that plan unspent, failing the USDT quote 201×/day and freezing dollar-denominated holdings at the previous close.
- **`fetch_json` must raise on 5xx.** It historically handled only 4xx and 429, so a 500 fell through to `response.json()` and was returned as if it were data — an exhausted subscription read as a quiet day and marked backfills converged on zero rows. Quota exhaustion is detected on the response *body* (`quota.looks_like_quota_error`), not the status code alone, so an ordinary origin blip cannot pause a whole plan for the day.
- **`ts` is never part of a unique key.** It is a Gregorian mirror of the Jalali domain key, derived by `JalaliDerivedDateTime.pre_save` purely as a partition dimension, and only `StockTransactionTick` is actually a hypertable. When it sat inside the constraints on `MarketCandle`/`DailyStockHistory`, a later correction to the derivation formula (Tehran midnight is 20:30 UTC the *previous* day) meant every re-ingest inserted a second copy instead of conflicting: 3.7M duplicate candles and 495k duplicate history rows, 380k disagreeing on close price, making risk/return output non-reproducible. `pre_save` now always recomputes and never honours a caller-supplied value. Migration `0005` purged the data; the earlier repair (`0041`, squashed) fixed only the data and the duplicates came straight back.
- **A property is a series of dated marks, not a position.** `HOUSE_MARK_KINDS` (opening + valuation mark) is the one place that pair is declared, and `_projection_state` REPLACES on each mark rather than accumulating. Three rules follow, each of which shipped broken: which kind to write is an **account**-level question — every opening carries `tracking_started_at`, so a property added after tracking began must be a `VALUATION_MARK`, not a second opening (deciding per-asset raised a `LedgerError` that escaped as a 500 on the holdings screen the moment a portfolio held two properties); deleting a property must reverse **every** live mark, since reversing only the opening leaves later marks to rebuild the holding on the next replay; and any endpoint that shows `area_sqm` must also accept it — `LedgerEntryPatchSerializer` took the field, dropped it, and answered 200, so a resize looked saved and was not.
- **The integrity gate and `_build_returns_matrix` must agree.** A leading run of absent sessions is the *absence* of history (newly listed), not a hole in it — `integrity.compute_symbol_integrity` clamps to `first_observed` exactly as `returns._gap_profile` does. Per-symbol halts come from `calendars.symbol_halt_days` (zero volume AND zero trades for one symbol while the market traded), the single-symbol twin of `market_closure_days`. `excessive_rejections` needs both `MIN_REJECTIONS_FOR_GATE` and the ratio — a bare 1% bar over a ~112-session window means "one bad day disqualifies you" and dropped 208 symbols. `insufficient_backfill` (our warehouse is incomplete) is a distinct reason code from `low_coverage` (the data is gappy); conflating them reported 79% of the universe as corrupt when most of it was simply un-fetched.

## Frontend conventions

- Pages compose `ui.jsx` primitives (`Card`, `StatTile`, `Async`, `Table`, `Button`, `PageHeader`, `Disclosure`) and write no bespoke panel/loading/error markup.
- Every element a test or screen reader needs takes `testId` → `data-testid="<page>-<thing>"`, lowercase-hyphenated.
- Data fetching goes through `useApi(fn, deps, opts)` (`useApi.js`) — the one fetch pattern (loading/error/poll/pause-when-hidden). Never hand-roll a fetch effect.
- `res.statusText` is always `""` over HTTP/2, which is what production serves. Never use it as an error message's only fallback, or every unexplained failure reaches the user as a bare "Something went wrong."
- Tooltip formatters in `charts.jsx` return **HTML**, and the labels they print (holding nicknames) are user-typed. Everything user-supplied goes through `esc()` — an unescaped `<` silently swallows the row.
- Charts go through `components/charts.jsx` wrappers only — pages never `import echarts` directly and never write a hex color; colors come from `var(--c-*)` tokens in `index.css`, resolved via `useChartTokens()`.

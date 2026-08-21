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
- Every price-resolution path — the returns matrix, `value_as_of` (feeds TWR cash-flow boundaries) AND `compute_dynamic_net_worth_series` (feeds the chart) — must bound forward-fill at 5 trading sessions via the one helper `marketdata.calendars.sessions_between()`, and exclude the asset with `price_gap_exceeded` beyond it. A bare `date__lte(...).order_by("-date").first()` silently values today's holding at a delisted asset's last close. Two ways to get the bound wrong, both of which shipped at once: counting **calendar days** (a closure or the Thu/Fri weekend ages a price faster than the market does, so the guard fires early and drops a live holding — this produced a fake two-day portfolio cliff on 2026-07-06/07), and measuring against **one symbol's own rows** (that symbol's latest row is by construction the newest at-or-before `as_of`, so the count is always 0 and the guard can never fire). The calendar must be market-wide and measured in sessions.
- `SnapshotListView`'s synthetic fallback must trigger only when there is genuinely nothing recorded. Comparing observed snapshot days against *calendar* days in the window is permanently true — markets are shut Thursday and Friday — so it discarded real history on every request for every holdings-only account.
- Before deploying a squashed migration, diff its `replaces` list against the target's `django_migrations` and confirm the generated file defines `Migration` exactly once. A partially-applied squash is silently discarded in favour of migrations that no longer exist on disk, and a duplicated class definition drops `replaces` entirely — both fail the deploy at `CreateModel`, not at `migrate --check`.
- The F1 TSE price-unit guard belongs inside `daily_returns_matrix()` (`portfolio/services/returns.py`), not in `optimization.py` — all ~8 mixed-unit consumers (diagnostics/AnalyticsView, AssetReturnsView, market catalog, `nightly_asset_metrics`, warmup_cache) route through that one function; guarding only the optimizer leaves the rest computing/caching/persisting cross-unit numbers that may be 10× wrong.
- Commodity/crypto/option history is sourced from live-fetch organic ingest in `portfolio.tasks`, retired from the archive backfill path (`_RETIRED_ARCHIVE_ENDPOINTS` in `marketdata/archive.py`). Never re-add them to `_FULL_HISTORY`/`_ENDPOINT_PRIORITY` or they fall through to the gold fetch branch.
- Monetary storage standardization on Rial was tried (2026-08-05) and **reverted** (2026-08-06): `marketdata/ingest.py` writes every BrsAPI field verbatim with zero multipliers (the warehouse is a faithful copy of the provider payload, unit label included). `marketdata/currency.py:to_toman()` is the only conversion helper, used solely by `portfolio/live/extractor.py` to blend sources into the Toman-denominated `portfolio_price`. Never call it from `ingest.py`; never reintroduce a blanket `x10`/manifest-multiply on warehouse tables.
- All asset-eligibility gating for the returns matrix (`price_gap_exceeded`, `insufficient_history`, `insufficient_coverage` at the 0.90 bar) lives in `_build_returns_matrix()` (`portfolio/services/returns.py`). `optimize()` should only add its own `min_observations`/zero-variance filter before `dropna(how="any")` — a coverage filter there is dead code shadowing the upstream reason codes the UI surfaces.
- When multiple call sites independently re-implement the same validation/threshold check with different magic numbers (e.g. duplicated "bad price" spike-detection heuristics), consolidate into one shared module/constant rather than patching each call site.

## Frontend conventions

- Pages compose `ui.jsx` primitives (`Card`, `StatTile`, `Async`, `Table`, `Button`, `PageHeader`, `Disclosure`) and write no bespoke panel/loading/error markup.
- Every element a test or screen reader needs takes `testId` → `data-testid="<page>-<thing>"`, lowercase-hyphenated.
- Data fetching goes through `useApi(fn, deps, opts)` (`useApi.js`) — the one fetch pattern (loading/error/poll/pause-when-hidden). Never hand-roll a fetch effect.
- Charts go through `components/charts.jsx` wrappers only — pages never `import echarts` directly and never write a hex color; colors come from `var(--c-*)` tokens in `index.css`, resolved via `useChartTokens()`.

# portfolio-saas

Subscription, real-time portfolio tracker for the Iranian market (BRS gold/currency/crypto + TSETMC stocks).
Multi-user from day one: prices are fetched once globally and shared across every user (see README "Why it scales").

**Stack:** React (JS, Vite, Tailwind 4, echarts) + Django REST + PostgreSQL/TimescaleDB + Redis + Celery, Docker Compose.
Full API surface, layout tree, and prod deploy steps: [`README.md`](README.md). Price unit policy: [`docs/F1_POLICY.md`](docs/F1_POLICY.md).

## Layout

- `backend/config/` — Django project (settings, urls, celery, health)
- `backend/accounts/` — User model + JWT auth
- `backend/portfolio/` — user-portfolio domain: models (Asset, Account, Holding, Price, Snapshot, LedgerEntry), `services/` (valuation, ledger, performance, returns, diagnostics, optimization), `live/` (price loop: fetcher, extractor, redis_client), `tasks.py` (Celery heartbeat)
- `backend/marketdata/` — market-history warehouse, separate bounded context: symbol-keyed tables, no user FKs, BrsApi fetchers, ingest, archive/quota-driven backfill, staff-only Ops console backend
- `frontend/src/` — `pages/` (Dashboard, Ops, MyOptimal, BestOverall, Family, Ledger, Onboarding), `components/ui.jsx` (shared primitives: Card, StatTile, Async, Table, Button…), `components/charts.jsx` (themed echarts wrappers), `api.js` + `useApi.js` (the one fetch pattern), `format.js` (number/date formatting)
- `archive/` — legacy Excel pipeline, reference only, not active

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
- Every price-resolution path — the returns matrix AND `value_as_of` (feeds TWR cash-flow boundaries) — must bound forward-fill at 5 trading sessions counted from the warehouse's distinct-date session calendar, and exclude the asset with `price_gap_exceeded` beyond it. A bare `date__lte(...).order_by("-date").first()` silently values today's holding at a delisted asset's last close.
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

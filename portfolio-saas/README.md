# portfolio-saas

Real-time portfolio tracker for the Iranian market (BRS gold/currency/crypto +
TSETMC stocks). Multi-user from day one: **prices are global, not per-user**, so
one fetch updates everyone's valuation and fetch cost is O(sources), not O(users).

This is the only project in the repository; `.github/workflows/` stays at the
repo root because GitHub requires it.

**Stack:** React (Vite, Tailwind, echarts) · Django REST · PostgreSQL/TimescaleDB
· Redis · Celery · Docker Compose. Pricing lives in
`backend/portfolio/live/extractor.py`, parity-checked against a frozen oracle
under `backend/tests/legacy_oracle/`. Unit/price policy and warehouse
safeguards: [`docs/REFERENCE.md`](docs/REFERENCE.md).

## Quick start (dev)

```bash
# from portfolio-saas/ — create .env with your BRS + TSETMC keys (gitignored)
docker compose up --build
```

- Frontend: http://localhost:5173 · API: http://localhost:8000/api/
- Demo users seeded on first boot (DEBUG only): **demopro@portfolio.local /
  demopro12345**, **demofree@portfolio.local / demofree12345**,
  **admin@portfolio.local / admin12345**.

### Without Docker

```bash
# backend (needs local Postgres + Redis)
cd backend && pip install -r requirements.txt -r requirements-dev.txt
export POSTGRES_HOST=localhost POSTGRES_USER=$USER POSTGRES_DB=postgres
export REDIS_URL=redis://localhost:6379/1 CELERY_BROKER_URL=redis://localhost:6379/2
python manage.py migrate && python manage.py seed_assets && python manage.py seed_demo
python manage.py runserver
celery -A config worker -l info &  celery -A config beat -l info &

# tests — local Postgres required (DISTINCT ON; sqlite cannot run them)
python -m pytest -q

# frontend (proxies /api to :8000)
cd frontend && npm install && npm run dev
npx playwright test          # e2e, needs E2E_EMAIL / E2E_PASSWORD
```

## Layout

```
portfolio-saas/
├── docker-compose.yml            # dev (Vite + gunicorn/uvicorn)
├── docker-compose.prod.yml       # prod (frontend joins vps-edge)
├── docs/REFERENCE.md             # price units, warehouse safeguards, data provenance
├── backend/
│   ├── config/                   # Django project: settings, urls, celery,
│   │                             #   health, api.py (JSON + error translation),
│   │                             #   observability.py (request id, Sentry, alerts)
│   ├── accounts/                 # User model + JWT auth
│   ├── portfolio/                # the user-portfolio domain:
│   │   ├── models.py             #   Asset, Account, Holding, Price, Snapshot, LedgerEntry
│   │   ├── services/             #   valuation, ledger, performance, returns,
│   │   │                         #   diagnostics, optimization
│   │   ├── live/                 #   price loop (fetcher, extractor, redis_client)
│   │   └── tasks.py              #   Celery heartbeat fetch_and_publish
│   ├── marketdata/               # market-history warehouse, a separate bounded
│   │   │                         # context: symbol-keyed, no user FKs
│   │   ├── models.py             #   history tables + Codal announcements
│   │   ├── fetchers.py           #   every BrsApi endpoint client
│   │   ├── endpoints.py          #   the endpoint registry they validate against
│   │   ├── ingest.py + tasks.py  #   payload->rows + the sync schedule
│   │   ├── archive.py + quota.py #   gap-driven backfill under a request budget
│   │   ├── calendars.py          #   which days a market was actually open
│   │   └── admin_api.py          #   staff-only /api/admin/* Ops console backend
│   └── tests/                    # 19 thematic suites, one per bounded concern
└── frontend/src/                 # pages/ · components/ui.jsx + charts.jsx
                                  # api.js + useApi.js (the one fetch pattern)
```

Each app has **one** squashed schema migration (`0001_squashed`); marketdata adds
`0002_storage_tuning` for autovacuum triggers and the TimescaleDB tick hypertable.

## Why it scales

Gold, USD, KAMA etc. have one price for everyone. The fetcher makes a bounded set
of provider calls (BRS gold/crypto/commodity + TSETMC symbols/options/ETF NAV, in
parallel), writes one `Price` row per asset, and every user's valuation reflects
it on their next read.

- **Latest prices** come from a Postgres `DISTINCT ON` over an `(asset, fetched_at)`
  index, then cached in Redis (`prices:latest`).
- **Valuation** is pure arithmetic (holdings × latest prices), no per-user network calls.
- **Write rate is bounded** by schedule frequency: one net-worth `Snapshot` per
  user per fetch (`bulk_create`), independent of how often prices move.

## Price loop

Celery beat ticks `fetch_and_publish` → fetch → atomic write of `Price` +
per-user `Snapshot` rows → bust the `prices:latest` and returns caches. The task
enforces its own cadence from whether the TSE is open (`marketdata/market_state.py`),
so it runs far more often during a session than overnight. The frontend polls
`/api/valuation/` every 60s.

The task keeps the `fetch_and_publish` name for beat-schedule stability; it no
longer publishes (the SSE view and Redis fan-out were removed — nothing
subscribed). `manage.py fetch_prices` runs the same body manually. Staleness is
watched at `/api/health/prices/` (503 when the freshest price is older than 15
min); an on-VPS cron restarts Celery on failure.

Three Celery services: `live` (price loop and lightweight producers), `archive`
(warehouse backfill plus Codal extraction), and `beat`.

## Accounts

Every API request needs a JWT; tokenless requests get 401. There are no
subscription tiers — every endpoint is available to any authenticated user. An
"account" is a named portfolio group ("Main", "Brokerage", "Cash"); the Iranian
market has no Plaid equivalent, so v1 uses manual or imported holdings.

## API summary

Every path needs a JWT unless marked `–`. The `/api/market/*` read surface was
removed; the warehouse is now read only through the analytics and optimization
services.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/auth/register/` · `/api/auth/login/` | – | create account / JWT pair |
| POST | `/api/token/refresh/` | refresh | new access token |
| GET | `/api/auth/me/` | JWT | current user |
| GET | `/api/assets/` | JWT | asset catalog |
| GET/POST | `/api/accounts/` | JWT | list / create accounts |
| GET/PATCH/DELETE | `/api/accounts/<id>/` | JWT | account detail |
| GET/POST | `/api/accounts/<id>/holdings/` | JWT | list / add holding |
| GET/PATCH/DELETE | `/api/accounts/<id>/holdings/<id>/` | JWT | edit / remove holding |
| GET/POST | `/api/accounts/<id>/ledger/` | JWT | ledger entries |
| POST | `/api/accounts/<id>/trades/` | JWT | buy / sell |
| GET | `/api/accounts/<id>/performance/?basis=` | JWT | TWR / XIRR / cost basis |
| GET | `/api/valuation/` | JWT | live net worth across accounts |
| GET | `/api/snapshots/?days=` | JWT | net-worth history (for charts) |
| GET | `/api/analytics/` | JWT | risk diagnostics + correlation |
| GET | `/api/optimization/my-optimal/` · `/frontier/` · `/best-overall/` | JWT | optimizer surfaces |
| GET/POST | `/api/admin/*` | staff | Ops console (overview, workflows, archive states, asset evidence) |
| GET | `/api/health/` · `/api/health/ready/` · `/api/health/prices/` | – | liveness / readiness / feed staleness |

## Production

Portfolio keeps its own private database, Redis, backend, frontend and Celery
workers. Only its frontend joins the existing `vps-edge` Docker network. TLS
termination is a VPS-wide reverse proxy at `/opt/apps/vps-edge` and is not
shipped from this repository.

```bash
cp .env.production.example .env.production
chmod 600 .env.production

# The reverse proxy must already be running (docker network vps-edge).
BACKUP_PASSPHRASE_FILE=/root/secrets/portfolio-backup-passphrase scripts/deploy.sh
docker compose -f docker-compose.prod.yml exec backend python manage.py seed_assets
curl https://portfolio.example.com/api/health/            # -> ok
```

The passphrase file must live outside the repository with mode `400` or `600`.
The first deployment skips the pre-deploy backup because no database exists yet;
later deploys refuse to continue unless the encrypted backup succeeds.

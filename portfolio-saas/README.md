# portfolio-saas

Subscription, real-time portfolio tracker for the Iranian market (BRS
gold/currency/crypto + TSETMC stocks). Multi-user from day one, built to scale:
prices are fetched once globally and shared across every user.

**Stack:** React (JS) + Django REST + PostgreSQL + Redis + Celery, orchestrated
by Docker Compose. Pricing lives in `backend/portfolio/live/extractor.py`
(parity-checked against a frozen oracle under `backend/tests/legacy_oracle/`).

## Quick start (dev)

```bash
# from portfolio-saas/
# optional local Python (3.11): python3.11 -m venv .venv && source .venv/bin/activate
#   pip install -r backend/requirements.txt
# create .env with your BRS + TSETMC API keys (gitignored)
docker compose up --build
```

Unit/price policy: see [`docs/F1_POLICY.md`](docs/F1_POLICY.md).

- Frontend: http://localhost:5173
- API: http://localhost:8000/api/

Demo users seeded on first boot (DEBUG only): **demopro@portfolio.local / demopro12345**, **demofree@portfolio.local / demofree12345**, **admin@portfolio.local / admin12345**.

## Production

Portfolio keeps its own private database, Redis, backend, frontend, and Celery
workers. Its frontend alone joins `vps-edge`; the independent edge stack under
`deploy/edge/` terminates TLS and routes each domain to its own frontend. Codal
document extraction runs directly from its dedicated worker and persistent
volume without a proxy.

```bash
cp .env.production.example .env.production
chmod 600 .env.production
# Start deploy/edge first (creates the vps-edge network), then:
BACKUP_PASSPHRASE_FILE=/root/secrets/portfolio-backup-passphrase ./scripts/deploy.sh
```

The passphrase file must live outside the repository with mode `400` or `600`.
The first deployment skips the pre-deploy backup because no database exists yet;
later deploys refuse to continue unless the encrypted backup succeeds.

## Why it scales

The single most important design choice: **prices are global, not per-user.**
Gold, USD, KAMA, etc. have one price for everyone. The fetcher makes a bounded
set of provider calls (BRS gold/crypto/commodity + TSETMC symbols/options/ETF
NAV, in parallel), writes one `Price` row per asset, and *every* user's
valuation reflects the update on their next read. Fetch cost is **O(sources)**,
not O(users).

- **Latest prices** are read via a Postgres `DISTINCT ON` over an
  `(asset, fetched_at)` index, then **cached in Redis** (`prices:latest`) —
  thousands of concurrent users hit the cache, not the DB.
- **Valuation** is pure arithmetic (`holdings × latest prices`), no per-user
  network calls.
- **Write rate is bounded** by schedule frequency: each fetch writes one net-worth
  `Snapshot` per user (`bulk_create`), independent of how often prices move.

## Price loop

Celery beat ticks `fetch_and_publish` → fetch → atomic write of `Price` +
per-user `Snapshot` rows → bust the `prices:latest` + returns caches. The task
enforces its own cadence based on whether the TSE is open (see
`marketdata/market_state.py`), so it runs far more often during a session than
overnight. The frontend polls `/api/valuation/` every 60s.

The task keeps the `fetch_and_publish` name for beat-schedule stability; it no
longer publishes. It once broadcast the price map over a Redis pub/sub channel
for SSE clients, but the frontend never subscribed, so the SSE view, the channel
fan-out and `/api/prices/stream/` were removed.

The `fetch_prices` management command calls the same body for manual runs.
Staleness is watched at `/api/health/prices/` (503 when the freshest price is
older than 15 min): an on-VPS cron restarts Celery on failure, and the hourly
GitHub Actions probe (`.github/workflows/fetch-prices.yml`) catches the site
being dark from outside. See `scripts/deploy.sh`.

## Accounts

**Authentication required:** every API request needs a JWT. Tokenless requests
get 401. There are no subscription tiers — `User.tier`, `pro_expires_at`,
`IsPro`/`RequiresFeature` and the payment integration were all removed, and every
endpoint is available to any authenticated user.

### "Account" = a named portfolio group

One user can have many accounts ("Main", "Brokerage", "Cash"). Real brokerage
linking plugs into `Account.broker` later — the Iranian market has no Plaid
equivalent, so v1 uses named groups with manual or imported holdings.

## API summary

| Method | Path | Auth | Notes |
|---|---|---|---|
Every path below needs a JWT unless marked `–`. The `/api/market/*` read surface
was removed with commit `2ea22be`; the warehouse is now read only through the
analytics and optimization services.

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

## Layout

```
portfolio-saas/
├── docker-compose.yml            # dev (Vite + gunicorn)
├── docker-compose.prod.yml       # prod (Caddy TLS + built frontend)
├── .github/workflows/fetch-prices.yml   # hourly dead-man's switch
├── backend/
│   ├── config/                   # Django project (settings, urls, celery, health)
│   ├── accounts/                 # User model + JWT auth
│   ├── portfolio/                # the user-portfolio domain:
│   │   ├── models.py             #   Asset, Account, Holding, Price, Snapshot, LedgerEntry
│   │   ├── services/             #   valuation, ledger, performance, returns, diagnostics, optimization
│   │   ├── live/                 #   price loop (fetcher, extractor, redis_client)
│   │   └── tasks.py              #   Celery heartbeat fetch_and_publish
│   └── marketdata/               # the market-history warehouse (separate bounded context):
│       ├── models.py             #   market-history tables (stocks, gold/FX, index, Codal
│       │                         #   announcements), symbol-keyed, no user FKs
│       ├── fetchers/             #   BrsApi endpoint clients (history, candles, codal, ...)
│       ├── ingest.py + tasks.py  #   payload->rows + daily/weekly sync schedule
│       ├── archive.py + quota.py #   gap-driven backfill under a provider request budget
│       └── admin_api.py          #   staff-only /api/admin/* Ops console backend
└── frontend/
    └── src/                      # React (auth, dashboard, analytics, optimization, market)
```

## Dev without Docker

```bash
# backend (needs local Postgres + Redis)
cd backend && pip install -r requirements.txt -r requirements-dev.txt
export POSTGRES_HOST=localhost POSTGRES_USER=$USER POSTGRES_DB=postgres
export REDIS_URL=redis://localhost:6379/1 CELERY_BROKER_URL=redis://localhost:6379/2
python manage.py migrate && python manage.py seed_assets && python manage.py seed_demo
python manage.py runserver
celery -A config worker -l info &  celery -A config beat -l info &

# tests (local Postgres required — DISTINCT ON; sqlite cannot run them)
python -m pytest -q

# frontend (proxies /api to :8000)
cd frontend && npm install && npm run dev
```

## Production commands

```bash
# Prepare application and edge configuration once.
cp .env.production.example .env.production
cp deploy/edge/.env.example deploy/edge/.env
chmod 600 .env.production deploy/edge/.env

# The edge owns public ports 80/443; each application remains a separate stack.
deploy/edge/deploy.sh
BACKUP_PASSPHRASE_FILE=/root/secrets/portfolio-backup-passphrase scripts/deploy.sh
docker compose -f docker-compose.prod.yml exec backend python manage.py seed_assets
curl https://portfolio.example.com/api/health/            # -> ok
```

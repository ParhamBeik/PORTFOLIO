# portfolio-saas

Subscription, real-time portfolio tracker for the Iranian market (BRS
gold/currency/crypto + TSETMC stocks). Multi-user from day one, built to scale:
prices are fetched once globally and shared across every user.

**Stack:** React (JS) + Django REST + PostgreSQL + Redis + Celery, orchestrated
by Docker Compose. Pricing logic is ported from `PORTFOLIO NEW STRUCTURE/src/`.

## Quick start (dev)

```bash
cp .env.example .env          # set BRS/TSETMC keys + Zarinpal merchant id
docker compose up --build
```

- Frontend: http://localhost:5173
- API: http://localhost:8000/api/
- SSE stream: http://localhost:8000/api/prices/stream/

A demo Pro user is seeded on first boot (DEBUG only): **demo@portfolio.local / demo12345**.

## Production

See `docker-compose.prod.yml` + `.env.production.example` + `DEPLOY.md`. Caddy
terminates TLS (auto Let's Encrypt), serves the built frontend, and
reverse-proxies `/api`. Target host: a Parspack VPS4 in an Iranian datacenter —
users and server share domestic routing, so no CDN layer is needed and
BrsApi.ir is directly reachable. Commands are at the bottom of this file.

## Why it scales

The single most important design choice: **prices are global, not per-user.**
Gold, USD, KAMA, etc. have one price for everyone. The fetcher makes one BRS +
one TSETMC call, writes one `Price` row per asset, and *every* user's valuation
reflects the update on their next read. Fetch cost is **O(sources)**, not O(users).

- **Latest prices** are read via a Postgres `DISTINCT ON` over an
  `(asset, fetched_at)` index, then **cached in Redis** (`prices:latest`) —
  thousands of concurrent users hit the cache, not the DB.
- **Valuation** is pure arithmetic (`holdings × latest prices`), no per-user
  network calls.
- **Write rate is bounded** by schedule frequency: each fetch writes one net-worth
  `Snapshot` per user (`bulk_create`), independent of how often prices move.

## Real-time

Celery beat ticks `fetch_and_publish` **every 2 minutes** → fetch → atomic write
of `Price` + per-user `Snapshot` rows → bust the `prices:latest` + returns caches
→ publish the price map once to the Redis pub/sub channel `prices:update`. The
API streams that single channel to every connected client over **SSE**
(`/api/prices/stream/`); each client recomputes its own portfolio value from the
pushed prices (push stays O(1) in user count). The frontend SSE client falls back
to 15s polling of `/api/prices/latest/` after 3 failures.

The `fetch_prices` management command calls the same body for manual runs.
Staleness is watched at `/api/health/prices/` (503 when the freshest price is
older than 15 min): an on-VPS cron restarts Celery on failure, and the hourly
GitHub Actions probe (`.github/workflows/fetch-prices.yml`) catches the site
being dark from outside. See DEPLOY.md.

## Accounts & subscriptions

- **Free tier:** real-time portfolio tracking — accounts, holdings, live net worth,
  net-worth history, global prices.
- **Pro tier:** portfolio **optimization** — risk diagnostics (volatility, Sharpe,
  Sortino, max drawdown, Calmar, VaR, CVaR, diversification ratio, correlation),
  optimization scenarios (Max Sharpe, Min Volatility, Risk Parity, HRP),
  rebalancing trades, and the efficient frontier. Plus the original rule-based
  insights.

`User.tier` (`FREE`/`PRO`) + `User.pro_expires_at` are the source of truth.
**Payments go through Zarinpal** (Iranian cards can't pay USD, so Stripe is not
used): `/api/billing/zarinpal/request/` → Zarinpal hosted page →
`/api/billing/zarinpal/callback/` verifies and activates an annual Pro
subscription. Pro is annual-prepay (Zarinpal has no native recurring billing).

### "Account" = a named portfolio group

One user can have many accounts ("Main", "Brokerage", "Cash"). Real brokerage
linking plugs into `Account.broker` later — the Iranian market has no Plaid
equivalent, so v1 uses named groups with manual or imported holdings.

## API summary

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/auth/register/` | – | returns user + JWT |
| POST | `/api/auth/login/` | – | JWT pair |
| POST | `/api/token/refresh/` | refresh | new access token |
| GET | `/api/auth/me/` | JWT | current user + tier |
| GET | `/api/assets/` | JWT | asset catalog |
| GET/POST | `/api/accounts/` | JWT | list / create accounts |
| GET/DELETE | `/api/accounts/<id>/` | JWT | account detail |
| GET/POST | `/api/accounts/<id>/holdings/` | JWT | list / add holding |
| GET/PATCH/DELETE | `/api/accounts/<id>/holdings/<id>/` | JWT | edit / remove holding |
| GET | `/api/accounts/<id>/valuation/` | JWT | account valuation |
| GET | `/api/valuation/` | JWT | live net worth across accounts |
| GET | `/api/snapshots/?days=` | JWT | net-worth history (for charts) |
| GET | `/api/prices/latest/` | JWT | global price map |
| GET | `/api/prices/history/?asset=` | JWT | per-asset series |
| GET | `/api/prices/stream/` | JWT | SSE live prices (`?token=`) |
| GET | `/api/insights/` | JWT + **Pro** | rule-based insights |
| GET | `/api/analytics/` | JWT + **Pro** | risk diagnostics + correlation |
| POST | `/api/optimization/` | JWT + **Pro** | `{scenario, constraints?}` → target weights + trades |
| GET | `/api/optimization/frontier/` | JWT + **Pro** | efficient frontier |
| GET | `/api/assets/returns/?days=` | JWT + **Pro** | return series + correlation |
| GET | `/api/market/history/?symbol=` | JWT | daily TSE close series (warehouse) |
| GET | `/api/market/candles/?symbol=&timeframe=` | JWT | OHLCV candles |
| GET | `/api/market/index/` · `/api/market/symbols/` | JWT | TSE index series / symbol metadata |
| GET | `/api/market/announcements/?symbol=` | JWT + **Pro** | Codal disclosures |
| GET | `/api/market/shareholders/?symbol=` | JWT + **Pro** | latest shareholder roster |
| POST | `/api/billing/zarinpal/request/` | JWT | create payment → `{redirect_url}` |
| GET | `/api/billing/zarinpal/callback/` | – | Zarinpal redirect target (verify) |
| GET | `/api/health/` · `/api/health/ready/` · `/api/health/prices/` | – | liveness / readiness / feed staleness |

## Layout

```
portfolio-saas/
├── docker-compose.yml            # dev (Vite + gunicorn)
├── docker-compose.prod.yml       # prod (Caddy TLS + built frontend)
├── .github/workflows/fetch-prices.yml   # hourly dead-man's switch
├── backend/
│   ├── config/                   # Django project (settings, urls, celery, health)
│   ├── accounts/                 # User + tier + pro expiry, JWT auth, IsPro
│   ├── portfolio/                # the user-portfolio domain:
│   │   ├── models.py             #   Asset, Account, Holding, Price, Snapshot
│   │   ├── services/             #   valuation + Pro analytics (returns, diagnostics, optimization, insights)
│   │   ├── live/                 #   2-min price loop (fetcher, extractor, pubsub, SSE)
│   │   └── tasks.py              #   Celery heartbeat fetch_and_publish
│   ├── marketdata/               # the market-history warehouse (separate bounded context):
│   │   ├── models.py             #   8 TSE/gold history tables, symbol-keyed, no user FKs
│   │   ├── fetchers/             #   BrsApi endpoint clients (history, candles, codal, ...)
│   │   ├── ingest.py + tasks.py  #   payload->rows + daily/weekly sync schedule
│   │   └── views.py              #   read-only /api/market/* endpoints
│   └── billing/                  # Zarinpal request/callback/verify
└── frontend/
    └── src/                      # React (auth, dashboard, analytics, optimization, billing)
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
cp .env.production.example .env.production   # DOMAIN, SECRET_KEY, Zarinpal, DB password
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
docker compose -f docker-compose.prod.yml exec backend python manage.py migrate
docker compose -f docker-compose.prod.yml exec backend python manage.py seed_assets
curl https://$DOMAIN/api/health/            # -> ok
```

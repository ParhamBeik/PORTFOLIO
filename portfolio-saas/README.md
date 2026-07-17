# portfolio-saas

Subscription-based, real-time portfolio tracker for the Iranian market
(BRS gold/currency/crypto + TSETMC stocks). Multi-user from day one, built to
scale.

**Stack:** React (JS) + Django REST + PostgreSQL + Redis, orchestrated by
Docker Compose. Pricing logic is ported from the existing
`PORTFOLIO NEW STRUCTURE/src/` engine.

## Quick start

```bash
cp .env.example .env          # set BRS/TSETMC keys (defaults already point at BRS)
docker compose up --build
```

- Frontend: http://localhost:5173
- API: http://localhost:8000/api/
- Admin: http://localhost:8000/admin/

A demo Pro user is seeded on first boot: **demo@portfolio.local / demo12345**.

## Why it scales

The single most important design choice: **prices are global, not per-user.**
Gold, USD, KAMA, etc. have one price for everyone. The fetcher makes one BRS +
one TSETMC call, writes one `Price` row per asset, and *every* user's valuation
reflects the update on their next read. Fetch cost is **O(sources)**, not
O(users).

Concretely:
- **Latest prices** are read once per request burst via a Postgres
  `DISTINCT ON` over an `(asset, fetched_at)` index, then **cached in Redis**
  for ~10s — so thousands of concurrent users hit the cache, not the DB.
- **Valuation** is pure arithmetic (`holdings × latest prices`), no per-user
  network calls.
- **Write rate is bounded** by schedule frequency: the cron writes one net-worth
  `Snapshot` per user per fetch (`bulk_create`), independent of how often prices
  move or users refresh.
- No Celery in v1 — the fetcher is a Django management command, so the scheduler
  (GitHub Actions now, Celery beat / EventBridge later) is swappable without
  touching logic. Redis is already wired as the cache and ready to be a broker.

## Keeping prices fresh

The fetcher is one command:

```bash
docker compose exec backend python manage.py fetch_prices
```

On a schedule, `.github/workflows/fetch-prices.yml` checks out the backend and
runs that same command against the production DB (`DATABASE_URL` secret). Add
these repo secrets: `DATABASE_URL`, `DJANGO_SECRET_KEY`, `BRS_API_KEY`,
`TSETMC_API_KEY`.

> GitHub Actions cron granularity is 5 minutes and imprecise. For tighter
> real-time, move the same command to a Celery beat worker — no code changes.

## Real-time in the UI

The frontend polls `/api/valuation/` every 15s (green "Live" pulse). This is
deliberately simple and works behind any cache. A WebSocket/SSE push layer is
the documented upgrade path when you outgrow polling — the valuation endpoint
already returns everything a push would carry.

## Accounts & subscriptions

- **Free tier:** real-time portfolio tracking (accounts, holdings, live value).
- **Pro tier:** advanced insights — allocation breakdown, concentration-risk
  alerts, gold target-band suggestions, net-worth trend. Gated by the
  `IsPro` permission on `/api/insights/`.

`User.tier` is the source of truth. The `/api/auth/upgrade/` endpoint flips it
for the demo; a **Stripe** webhook would call the same update in production.

### "Account" = a named portfolio group

One user can have many accounts ("Main", "Brokerage", "Cash"). Real brokerage
linking (OAuth / credentials) plugs into the `Account.broker` field later — the
Iranian market has no Plaid equivalent, so v1 uses named groups with manual or
imported holdings.

## API summary

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/auth/register/` | – | returns user + JWT |
| POST | `/api/auth/login/` | – | JWT pair |
| POST | `/api/token/refresh/` | refresh | new access token |
| GET | `/api/auth/me/` | JWT | current user + tier |
| POST / DELETE | `/api/auth/upgrade/` | JWT | set / clear Pro |
| GET | `/api/assets/` | JWT | asset catalog |
| GET/POST | `/api/accounts/` | JWT | list / create accounts |
| GET/DELETE | `/api/accounts/<id>/` | JWT | account detail |
| GET/POST | `/api/accounts/<id>/holdings/` | JWT | list / add holding |
| DELETE | `/api/accounts/<id>/holdings/<id>/` | JWT | remove holding |
| GET | `/api/valuation/` | JWT | live net worth |
| GET | `/api/prices/latest/` | JWT | global price map |
| GET | `/api/prices/history/?asset=` | JWT | per-asset series |
| GET | `/api/insights/` | JWT + **Pro** | advanced insights |

## Layout

```
portfolio-saas/
├── docker-compose.yml
├── .github/workflows/fetch-prices.yml   # cron -> manage.py fetch_prices
├── backend/
│   ├── config/                          # Django project (settings, urls)
│   ├── accounts/                        # User + tier, JWT auth
│   ├── portfolios/                      # Asset, Account, Holding, Price, Snapshot + valuation
│   └── pricing/                         # fetcher + extractor (ported engine), insights, commands
└── frontend/
    └── src/                             # React (auth, dashboard, insights)
```

## Dev without Docker

```bash
# backend
cd backend && pip install -r requirements.txt
export POSTGRES_HOST=localhost REDIS_URL=redis://localhost:6379/1
python manage.py migrate && python manage.py seed_assets && python manage.py seed_demo
python manage.py runserver

# frontend (proxies /api to :8000)
cd frontend && npm install && npm run dev
```

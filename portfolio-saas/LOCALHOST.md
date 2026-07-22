# Run the Portfolio app on your laptop (Docker Compose)

Head-to-toe local bring-up. When it finishes you'll have the full stack running
and **two ready-made sample portfolios** (Father's and Mother's) with 90 days of
price history, net-worth charts, buy/sell markers, and working optimization — so
you can click around and see the whole thing behaving before touching real data.

---

## 0. What you need (one-time)

- **Docker Desktop** installed and running. That's it — Postgres, Redis, Python,
  and Node all run *inside* containers, so you don't install any of them yourself.
- Check it's alive:

  ```sh
  docker --version && docker compose version
  ```

  Both printing a version = you're good.

> You do **not** need API keys to see the sample data. Keys (`BRS_API_KEY`,
> `TSETMC_API_KEY`) only power the *live* price fetcher for real market data —
> the samples ship with their own baked-in history. Add keys later (step 6).

---

## 1. Start everything

From the `portfolio-saas/` folder (the one with `docker-compose.yml`):

```sh
docker compose up --build
```

First run builds the images and takes a few minutes. You'll know it's ready when
the logs settle and you see the backend line about migrations/seeding finish.

This one command starts **six** services:

| Service         | What it is                                    | Port  |
|-----------------|-----------------------------------------------|-------|
| `db`            | PostgreSQL 16 (your data)                     | 5432  |
| `redis`         | Cache + Celery message broker                 | 6379  |
| `backend`       | Django API (runs migrations + seeds on boot)  | 8000  |
| `celery_worker` | Background jobs (price fetch, snapshots)      | —     |
| `celery_beat`   | Scheduler (ticks the fetch every ~2 min)      | —     |
| `frontend`      | React dev server (the website you open)       | 5173  |

---

## 2. Open the website

Go to <http://localhost:5173>

Log in with the sample family account:

```text
Email:    family@portfolio.local
Password: family12345
```

This account is **Pro tier**, so analytics and optimization are unlocked.

There is also a basic demo account if you want a simpler view:

```text
Email:    demo@portfolio.local
Password: demo12345
```

---

## 3. What you'll see

- **Dashboard** — total net worth across both portfolios, and the net-worth
  trend chart. The green/red dots on the line are the sample **buy/sell trades**.
- **Father's Portfolio / Mother's Portfolio** — click either account to open its
  detail page:
  - **Holdings** table (gold coins, cash, stock, crypto).
  - **Buy / Sell** panel — pick an asset, a side, a quantity, hit *Execute*. The
    value updates immediately and a new marker appears on the net-worth chart.
    Try selling more than you hold — it's rejected safely.
  - **Recent activity** — the trade ledger for that account.
  - **Price trend** — sparkline per holding from the seeded price series.
- **Insights / Analytics / Optimization** — the optimizer (max-Sharpe, min-vol,
  risk-parity) runs on the seeded 90-day return series.

---

## 4. Useful commands while it's running

Open a second terminal in the same `portfolio-saas/` folder.

```sh
# Watch just the backend logs
docker compose logs -f backend

# See which historical dates are thin (where to backfill next)
docker compose exec backend python manage.py coverage_report

# Re-seed deeper history for a better optimizer estimate (optional)
docker compose exec backend python manage.py seed_samples --days 180

# Django admin (browse every table): http://localhost:8000/admin
# Create an admin login first:
docker compose exec backend python manage.py createsuperuser
```

---

## 5. Stop / reset

```sh
# Stop (keeps your data)
docker compose down

# Stop AND wipe the database (fresh start — re-seeds on next `up`)
docker compose down -v
```

> `-v` deletes the `pgdata` volume. The sample portfolios are re-created
> automatically on the next `docker compose up` because seeding runs on boot.

---

## 6. (Optional) Turn on live market data

Real prices come from BrsApi. To enable the live fetcher:

1. Create a `.env` file next to `docker-compose.yml`:

   ```sh
   BRS_API_KEY=your_key_here
   TSETMC_API_KEY=your_key_here
   ```

2. Restart: `docker compose up -d`

`celery_beat` will start pulling live prices every ~2 minutes. The **backfill**
command fills in historical archive over time, working around API rate limits:

```sh
docker compose exec backend python manage.py backfill_market_data --all --days 365
```

Re-run `coverage_report` afterward to watch the archive fill in.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Port already in use (5173/8000/5432/6379) | Stop the conflicting program, or edit the `ports:` lines in `docker-compose.yml` |
| Login fails / no sample data | Seeding only runs when `DJANGO_DEBUG=1` (the default). Run `docker compose exec backend python manage.py seed_samples` — it prints "already exists" once seeded |
| Charts look empty | Hard-refresh the browser (Cmd/Ctrl+Shift+R) to clear a stale cache |
| Code changes not reflected | `docker compose up --build` |

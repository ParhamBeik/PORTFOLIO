# Deploying portfolio-saas (production)

Target topology: a **Parspack VPS4 Iran** (2 vCPU / 4 GB RAM / 60 GB SSD,
Ubuntu 24) running the full `docker-compose.prod.yml` stack behind **Caddy**
(auto-TLS). Server and users are both in Iran, so there is **no CDN layer** —
domestic routing is fast, and BrsApi.ir (the market data source) is reachable
without any sanctions workaround. This doc covers VPS preparation, the bring-up
sequence, and the dead-man's switch for the price feed.

## 0. Prerequisites

- Parspack **VPS4 Iran** (2 vCPU / 4 GB / 60 GB, Ubuntu 24). Smaller plans
  (VPS3, 2 GB RAM) can work with swap, but 4 GB gives comfortable headroom for
  the six containers plus on-VPS image builds.
- A domain (`app.example.com`) with an **A record pointing at the VPS IP**.
  Caddy cannot issue a cert until DNS resolves.
- Ports **80 and 443** open in the VPS firewall (`ufw allow 80,443/tcp`).
  Caddy uses both (80 for the ACME HTTP-01 challenge + redirect, 443 for the site).
- A live Zarinpal merchant ID (dashboard → obtain `merchant` UUID). Set
  `ZARINPAL_SANDBOX=0` for production.

## 1. Prepare the VPS (one-time)

```sh
# Docker + compose v2
curl -fsSL https://get.docker.com | sh

# Docker Hub image pulls can be blocked from Iranian IPs — use the ArvanCloud
# registry mirror:
cat > /etc/docker/daemon.json <<'JSON'
{ "registry-mirrors": ["https://docker.arvancloud.ir"] }
JSON
systemctl restart docker

# 2 GB swap: the Vite production build and the scientific-Python image build
# (numpy/scipy/cvxpy) can spike past 4 GB. Swap is the safety net, not the plan.
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Memory budget at steady state (fits 4 GB with room): gunicorn 3 async workers
(~500 MB), Celery worker `--concurrency=2` (~400 MB), beat (~100 MB), Postgres
defaults (~300 MB), Redis (~50 MB), Caddy + frontend nginx (~50 MB).

> Alternative to building on the VPS: build images locally / in CI and push to a
> registry. More setup; only worth it if builds start OOMing even with swap.

## 2. Configure

```sh
cp .env.production.example .env.production
# Fill in: DOMAIN, ALLOWED_HOSTS, CORS_ORIGINS, DJANGO_SECRET_KEY,
#          POSTGRES_PASSWORD, ZARINPAL_*, BRS_API_KEY, TSETMC_API_KEY
```

`DJANGO_SECRET_KEY`: `python -c "import secrets; print(secrets.token_urlsafe(60))"`.

The compose file marks the critical vars with `:?`, so it **refuses to boot** if
any is missing. settings.py additionally **refuses to boot** with `DEBUG=0` plus
a localhost CORS origin or the default secret key — fail closed.

## 3. Bring up the stack

```sh
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
```

Order is enforced by `depends_on` healthchecks: `db`+`redis` healthy → `backend`
healthy (it runs `migrate`, seeds the asset catalog, `collectstatic`; the demo
user is gated behind `DEBUG` and is **not** created here) → Celery + frontend →
Caddy. Caddy obtains the Let's Encrypt cert on first request.

Verify:

```sh
curl -fsS https://app.example.com/api/health/         # {"status":"ok"}
curl -fsS https://app.example.com/api/health/ready/   # {"status":"ready","checks":{...}}
curl -fsS https://app.example.com/api/health/prices/  # {"status":"fresh",...} after ~2 min
```

If readiness reports `degraded`, check `docker compose -f docker-compose.prod.yml --env-file .env.production logs backend`.

Then load the historical warehouse (one-time, ~minutes; idempotent — safe to re-run):

```sh
docker compose -f docker-compose.prod.yml --env-file .env.production exec backend \
  python manage.py backfill_market_data --all --days 365
```

After this, the daily Celery beat sync (`marketdata-daily-sync`, after TSE
close) keeps the warehouse current.

## 4. Dead-man's switch for the price feed

Celery beat fetches prices every 2 min. If beat/worker die quietly, prices go
stale while everything else looks healthy. Two watchers, one signal —
`/api/health/prices/` returns **503 when the freshest price is older than 15 min**:

1. **On-VPS self-heal cron** (the fix lives where the failure lives). As root:

   ```sh
   cat > /etc/cron.d/price-feed-watch <<'CRON'
   */10 * * * * root curl -fsS -m 10 http://localhost/api/health/prices/ >/dev/null || (cd /opt/portfolio-saas && docker compose -f docker-compose.prod.yml --env-file .env.production restart celery_worker celery_beat)
   CRON
   ```

   Adjust `/opt/portfolio-saas` to your checkout path.

2. **GitHub Actions external probe** (`.github/workflows/fetch-prices.yml`) —
   hourly `curl` against `https://$DOMAIN/api/health/prices/`. It catches what
   the on-VPS cron can't: the entire VPS being down. A red X in the Actions tab
   is the alert. It needs no secrets besides the `DOMAIN` variable and never
   touches the database (GitHub runners can't reach an Iran-hosted Postgres
   anyway, and exposing the DB publicly just for CI would be a bad trade).

`manage.py fetch_prices` still exists for manual/dev runs of the same fetch the
beat task performs.

## 5. Operations

- **Logs** are stdout-only (12-factor): `docker compose -f docker-compose.prod.yml --env-file .env.production logs -f backend`.
- **Prices** are fetched centrally by Celery beat (every ~2 min) and published to
  Redis; one fetch updates every user.
- **Market history** syncs daily after TSE close (beat task `marketdata-daily-sync`);
  symbol metadata refreshes weekly (Friday, market closed). Re-run
  `backfill_market_data` any time — unique constraints make it idempotent.
- **Backups**: snapshot the `pgdata` volume. `docker run --rm -v portfolio_pgdata:/d alpine tar czf - -C /d . > pg.tgz`.
- **Pro tier** is annual-prepay in Toman via Zarinpal (no native recurring
  billing); `activate_pro` sets `pro_expires_at = now + 365d` and is idempotent.
- **Upgrade**: `git pull && docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build`. Migrations run on `backend` start.

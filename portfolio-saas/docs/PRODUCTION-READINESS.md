# Production readiness — working checklist

Durable notes for the production-readiness goal. Resume from this file after
interruption. Status is evidence against the current tree, not intent.

Started: 2026-09-08. Branch: `main` @ `a6d2bdf`.
Heads up: another agent session was also working in this folder since
2026-09-08 17:48:15; run `git status` / `git diff` before large edits. Current
tree is this goal's diffs plus untracked `dump.rdb` (leave it).

## Final verification — 2026-09-10

- Backend: **1127 passed, 1 skipped** in the clean pinned Python 3.11
  environment; Ruff and `git diff --check` pass.
- Frontend: ESLint passes, Node **26/26**, Vitest **42/42** across eight files,
  and the production Vite build succeeds. The chart chunk remains 642.9 kB
  (214.52 kB gzip); splitting it was deferred because the measured live mobile
  performance score is already 100.
- VPS: deployed revision `203fdfd`; backend, frontend, both workers, beat,
  PostgreSQL, Redis, and MinIO report healthy. Public readiness is HTTP 200 with
  database/cache ready; prices are fresh (77 seconds against a 900-second
  threshold). The strict edge CSP remains active.
- SMTP is still the concrete external launch blocker: production resolves to
  localhost port 25 with no TLS/SSL and the socket returns
  `ConnectionRefusedError`. No email was sent.
- Production-writing Playwright remains intentionally unrun until a staging
  environment or disposable account exists.

## Recovery verification — 2026-09-09

Recovered Claude session `986c6a96-eb3a-4ecf-9bb0-81785979a0dc` from
`~/.claude/projects/-Users-parham-Downloads-GITHUB-PROJECTS-API-PORTFOLIO/`.
The transcript contains the implementation, tool results, errors and operator
decisions; no separate plan was found. Its final unanswered tool call at
2026-09-08 21:22:47 UTC was the full test/build sweep. This section closes that
interrupted verification, not the production launch.

- Preserved `main` at `a6d2bdf`, all 78 tracked-file diffs and six untracked
  files. No commit, push or deployment; the operator's production-writing e2e
  exclusion remains in force.
- Re-ran the backend suite in `/tmp/pa-venv` with an isolated test database:
  **1127 passed, 1 skipped**, 114.78 seconds. All 25 production dependency pins
  match this environment; `pip check` passes. The skip is an empty optional
  optimizer parameter set; four warnings come from OSQP deprecations.
- Ruff and ESLint pass; Node **26/26**, Vitest **42/42**, Vite build passes.
  Shell syntax and `git diff --check` pass.
- Closed the pending container-header verification on the VPS using the local
  build and unchanged nginx configuration mounted read-only into an isolated
  `nginx:1.31.4-alpine` container with no network or published ports. Document
  and reset-route security headers and `no-cache` pass; a hashed JavaScript
  asset retains security headers, immutable caching and gzip. This tests the
  runtime configuration, not a fresh multi-stage Docker build or Lighthouse run.
- Read-only production recheck: revision `a6d2bdf`, backend healthy, readiness
  reports database/cache ready, and the edge still serves strict `script-src
  'self'`. Mail remains Django's default SMTP backend at `localhost:25`, with
  **ConnectionRefusedError**. No email was sent and no production data changed.

Remaining: obtain a real relay and verified sender, authorize deployment of the
working tree, configure the relay, then verify receipt and completion of a reset
for an operator-controlled account. A successful TCP connection alone does not
verify SMTP authentication, TLS, message acceptance or inbox delivery. The old
local `.venv` remains preserved; use the verified pinned environment above until
it is replaced. Staging e2e and fresh Lighthouse remain unverified in this recovery.

## Objective (do not shrink)

A secure, reliable, fast, accessible, polished application with complete user
journeys and a credible growth path; a clear, consistent codebase. Complete when
every first-party file is accounted for, critical journeys are verified,
identified release blockers are resolved, and final applicable checks pass.

**Not launch-ready, blocked on one external dependency.** Password reset is
**confirmed** broken in production (measured 2026-09-09): the code that reads
the mail configuration is not deployed, and there is no relay for it to read —
no MTA on the box, no SMTP credentials in any stack, and a self-hosted relay on
a bare IP would not deliver. Closing it needs a transactional-mail account the
owner must open; every part that is code has been written and verified.

Everything else measurable now measures well: Lighthouse Performance 100 against
live production, 1,127 backend tests against the pinned dependency set, both
linters clean, and the edge CSP hardened. A review/plan is not completion.

## Corrections to earlier batches (batch 5)

Two entries in the old baseline were wrong, and both understated the tree:

- **pip-audit IS in CI.** `.github/workflows/ci.yml` has an `audit` job that
  pins pip/setuptools to the Dockerfile builder's versions, installs
  `requirements.txt`, and runs `pip-audit`, plus `npm audit --omit=dev
  --audit-level=moderate`. It runs on push, PR and a Monday cron, and is
  deliberately outside `deploy`'s `needs`.
- **No backups or restore drill.** Backup production was removed for the VPS
  storage baseline; the database lives only in the `pgdata` volume.

The remaining launch gap is SMTP — and it is BOTH deployment configuration and
code: the `EMAIL_*` block that reads that configuration is still uncommitted, so
deploying it is step one and pointing it at a relay is step two.

## Baseline (measured 2026-09-08/09, batch 5, on this machine)

| Check | Result | How |
|---|---|---|
| Backend suite, **pinned deps** | **1127 passed, 1 skipped** | clean py3.11 venv off `requirements.txt` + `-dev.txt` |
| Backend suite, local `.venv` | 1077 passed, 1 skipped | see the drift warning below |
| Ruff | **clean** | `ruff check .` in `backend/`, config `backend/ruff.toml` |
| Missing migrations | **none** | `makemigrations --check --dry-run`, fresh database |
| pip-audit, **pinned deps** | **0 vulnerabilities** | clean venv off `requirements.txt`, as the CI job does |
| npm audit (prod) | **0 vulnerabilities** | `npm audit --omit=dev --audit-level=moderate` |
| ESLint | **0 errors, 0 warnings** | `npm run lint`, config `frontend/eslint.config.js` |
| Vitest | **41 passed (7 files), 3 runs** | `npm run test:vitest` |
| Node unit | **26 passed** | `npm run test:unit` |
| Vite build | **succeeds** | `npm run build` |
| API query counts | **flat in holdings** | probe at 3 vs 30 holdings, below |
| Lighthouse performance | **100** | run against live production from the VPS |
| Live SMTP | **broken, confirmed** | socket refused from the backend container |

Prior batches: Playwright 26 passed / 10 skipped.

### Warning: the local `.venv` is not what ships

`portfolio-saas/.venv` differs from `backend/requirements.txt` on **18 of 25
pinned packages** and is missing three outright (`boto3`, `openpyxl`,
`beautifulsoup4` — the Codal parsing deps, which is why it collects 5 fewer
tests). Notable drift: simplejwt 5.3.1 vs 5.5.1, requests 2.32.3 vs 2.34.2,
redis 5.0.8 vs 8.1.0, psycopg 3.2.1 vs 3.3.5, celery 5.4.0 vs 5.6.3.

Two consequences, both of which bit during this batch:

- A suite that passes in `.venv` is **not** evidence about the deployed
  artifact. The 1127-test run above is; the 1077 one is not.
- `pip-audit` run inside `.venv` reported 16 vulnerabilities across five
  packages — every one of them either already fixed by the pin in
  `requirements.txt` (simplejwt PYSEC-2026-1305 is fixed in 5.5.1, which is what
  is pinned) or build/dev tooling that never ships. This is the exact failure
  the CI audit job's comment warns about. Audit a clean install of
  `requirements.txt`, never an ambient environment.

Recreating `.venv` from the pinned files is the fix.

### Query-count probe (batch 5, throwaway test, not committed)

Queries per request at 3 vs 30 holdings — no endpoint scales with holdings:

| Endpoint | 3 holdings | 30 holdings |
|---|---:|---:|
| `/api/valuation/` | 9 | 5 |
| `/api/accounts/` | 3 | 3 |
| `/api/assets/` | 1 | 1 |
| `/api/snapshots/?days=30` | 11 | 11 |
| `/api/insights/` | 6 | 6 |
| `/api/accounts/<id>/holdings/` | 2 | 2 |
| `/api/accounts/<id>/ledger/` | 4 | 4 |
| `/api/accounts/<id>/performance/` | 1 | 1 |

(The 9→5 on valuation is cache warming on the first call, not an N+1.)

## Measured against LIVE production (2026-09-09)

`https://portfolio-89-106-206-4.sslip.io`, deployed revision `a6d2bdf`. All
read-only. Docker on the dev Mac crash-loops on an 8 GiB host, so everything
below was run from the VPS instead.

| Check | Result |
|---|---|
| `/api/health/`, `/ready/`, `/prices/` | ok · ready (db+cache) · fresh, 46 s old |
| Lighthouse **Performance** | **100** (mobile) |
| Lighthouse Accessibility | 98 |
| Lighthouse Best Practices / SEO | 100 / 100 |
| LCP · FCP · Speed Index | 1.3 s · 1.3 s · 1.3 s |
| Total Blocking Time | 10 ms |
| Cumulative Layout Shift | **0** |

Route splitting confirmed working: the sign-in page loads only `index-*.js`
(89 KB transferred) and the stylesheet. The 643 KB `charts` chunk is **not**
on the anonymous critical path. `unused-javascript` flags 52 KiB of the shell
that the login route does not exercise, which is inherent to an SPA and did not
stop Performance reaching 100.

The one accessibility failure is `landmark-one-main` on the sign-in page —
**already fixed in the working tree** (`Auth.jsx`, `ResetPassword.jsx`,
`Legal.jsx` and `Shell.jsx` all wrap their content in `<main>`), just not
deployed. Expect 100 after the next deploy.

### P0 CONFIRMED: password reset is broken in production, and it is not a
### configuration oversight — the code that reads the config is not deployed

Measured from inside `portfolio-saas-backend-1`:

```
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST    = 'localhost'      EMAIL_PORT = 25
DEFAULT_FROM_EMAIL = 'webmaster@localhost'
socket.create_connection(('localhost', 25)) -> ConnectionRefusedError
```

Those are **Django's own global defaults**, not this project's. The entire
`EMAIL_*` block in `config/settings.py` is uncommitted working-tree work, so the
deployed revision never reads `EMAIL_HOST` from the environment at all — which
is why `DEFAULT_FROM_EMAIL` is `webmaster@localhost` and not the
`noreply@…` that `.env.production` sets. Every password-reset request today
returns 200, logs a failure, and sends nothing.

**Why every existing guard passed.** `docker-compose.prod.yml` declares
`EMAIL_HOST: ${EMAIL_HOST:?set EMAIL_HOST}` and
`DEFAULT_FROM_EMAIL: ${DEFAULT_FROM_EMAIL:?set DEFAULT_FROM_EMAIL}`, so compose
refuses to start without them. `.env.production` sets `EMAIL_HOST=localhost`,
which satisfies a presence check perfectly. A required variable holding a
placeholder passes everything that asks whether it is *set*; nothing asked
whether it *works*. The plumbing itself is correct — `environment: *backend-env`
does reach the backend, workers and beat — so once a real host is set the value
arrives where it should.

**This is a genuine external blocker, confirmed by search, not assumed.** On the
VPS: no MTA is installed (`postfix`/`sendmail`/`msmtp`/`exim4` all absent),
nothing listens on 25/587/465, and **no stack on the box has SMTP credentials** —
newsintel, bama and twitter-saas have no `EMAIL_*`/`SMTP_*`/provider keys at all.
So there is no relay to borrow and none to point at. Standing up Postfix on this
host is *not* a fix: mail from a bare VPS IP on an `sslip.io` name with no SPF,
DKIM, DMARC or forward-confirmed rDNS is rejected or spam-foldered by essentially
every provider, and for password reset deliverability is the entire feature. It
needs an account with a transactional provider (Postmark / Resend / SES /
Mailgun) — credentials only the owner can create.

Three steps, in order, and only the first two are code:

1. Deploy the `EMAIL_*` block that already exists in the working tree. Until
   then the deployed revision does not read `EMAIL_HOST` at all.
2. Set `EMAIL_HOST`/`EMAIL_PORT`/`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD` and a
   `DEFAULT_FROM_EMAIL` on a domain the relay is authorised to send for.
3. Confirm connectivity: the Ops console's **Outbound mail** row disappears (it renders only
   when the status is not healthy), and `deploy.sh` prints
   `ok outbound mail relay reachable at <host>` instead of its warning. Then
   verify an actual reset email arrives and its link completes a reset for an
   operator-controlled account; the socket probe alone cannot prove delivery.

### Housekeeping: two `.env.production` files exist on the VPS

`/opt/apps/portfolio-saas/.env.production` (mtime 2026-08-22) and
`/opt/apps/portfolio-repo/portfolio-saas/.env.production` (mtime 2026-09-08)
**differ**. `deploy.sh` derives `ENV_FILE` from its own location, so the
`portfolio-repo` copy is the live one and the `/opt/apps/portfolio-saas` copy is
a stale leftover. Editing the wrong one is a silent no-op; delete it or symlink
it once someone has confirmed nothing else reads it.

### P1 CONFIRMED: the edge proxy overwrites the app's CSP with a weaker one

The container and the browser do not receive the same policy:

| | `script-src` | `connect-src` | `Referrer-Policy` |
|---|---|---|---|
| what `portfolio-saas-frontend-1` sends | `'self'` | `'self' https://*.ingest.sentry.io` | `same-origin` |
| what a browser got, **before the fix** | `'self' 'unsafe-inline'` | `'self'` | `strict-origin-when-cross-origin` |
| what a browser gets **now** | `'self'` | `'self'` | `strict-origin-when-cross-origin` |

Caddy's `header` directive *replaces* an upstream header rather than deferring
to it, and `/opt/apps/vps-edge/Caddyfile` imports one `security_headers` snippet
shared by Portfolio, Bama and Twitter. `'unsafe-inline'` in `script-src` is the
directive that decides whether an injected `<script>` runs, and chart tooltips
build HTML out of user-typed holding nicknames — the exact surface CSP is the
second line of defence for.

The app does not need it: the shipped `index.html` has **one external module
script, zero inline bodies, zero `on*` handlers, zero `javascript:` URLs**, and
the container already serves the strict policy successfully. `bama-frontend`
also has zero inline scripts.

`connect-src` losing the Sentry origin is currently harmless — `VITE_SENTRY_DSN`
is not set — but it would silently break browser error reporting the day it is.

**Why CI could not catch this:** `frontend/scripts/lhci.sh` asserts
`script-src 'self'` against the container on a local port. That assertion passes
and is correct about the image; it is simply not about what ships. `deploy.sh`
now asserts the headers a browser receives from the live domain, and that check
was run against production and **correctly failed** on this exact issue.

**FIXED at the edge on 2026-09-09, with the operator's authorization.** The
audit that made it safe: `security_headers` was shared by Portfolio, Bama *and*
newsintel, and newsintel is Next.js with streaming server components — it serves
**7 inline `<script>` bodies** (React's `$RT`/`$RS`/`$RV` hydration runtime) on
its login page alone. Tightening the shared snippet as-is would have left the
news site unable to hydrate. Twitter was never at risk; it imports
`security_headers_x_media`.

So the snippet was split rather than simply tightened:

| Domain | snippet | `script-src` |
|---|---|---|
| Portfolio | `security_headers` | `'self'` — hardened |
| Bama | `security_headers` | `'self'` — hardened (0 inline, audited) |
| newsintel | `security_headers_news` (new) | `'self' 'unsafe-inline'` — Next.js needs it |
| Twitter | `security_headers_x_media` | untouched |

Procedure: backup to `Caddyfile.bak-precsp-20260908-211303`, `caddy validate`
("Valid configuration"), `caddy reload`, then re-verify. After the reload all
four sites answer 200, Portfolio and Bama report `script-src 'self'`, and the
page-level audit confirms Portfolio and Bama each load exactly one same-origin
external script with zero inline bodies — so the strict policy is satisfiable
by construction, not by luck. The `deploy.sh` header gate, which failed against
this before the change, now passes.

Narrow `security_headers_news` the day newsintel emits a CSP nonce.

### P1 CONFIRMED: refresh-token tables were growing without bound

Production, measured directly:

```
outstanding tokens        1,440   (310 already expired, 21.5%)
blacklisted tokens        1,019
table size                944 kB
oldest row                2026-07-25   (~6.5 weeks)
```

Nothing has ever deleted a row. `_revoke_all` iterated all 1,440 at two queries
each — **~2,880 round trips** inside a password change or account deletion, and
rising forever. Both fixes in this batch are validated by these numbers: the new
nightly task would drop 310 rows immediately, and `_revoke_all` is now 2 queries.

### P0 CONFIRMED: the disk projection overstated headroom by 22x, and its alert
### could not fire at all

The Ops console's only capacity signal. Measured on production, 2026-09-09:

```
CONSOLE SAID                          THE DEVICE ACTUALLY WAS
budget_gb        250                  total        147.4 GB
used_bytes        19.28 GB            used         109.8 GB  (74.5%)
target_80_bytes  200.00 GB            free          31.6 GB
days_to_80pct    257.1                growth      ~0.70 GB/day  (measured)
alert            False                days to 80%   11.6
                                      days to full  44.9
```

Two independent defects, compounding, both silent:

1. **A hand-typed denominator.** `VPS_DISK_BUDGET_GB` was 250, copied from an
   aspirational comment in `docker-compose.prod.yml` ("Dedicated 16 GB RAM /
   250 GB disk VPS"). The device is 147.4 GB.
2. **The wrong numerator.** It compared `pg_database_size` — one database's
   *logical* size — against a whole-filesystem budget. A filesystem fills from
   everything on it: three other stacks share this box, 9 GB is Docker images
   and build cache, and **16 GB is this app's own encrypted backups**, which
   grow in lockstep with the database they back up. `pg_database_size` saw
   19 GB of the 110 GB in use.

And the alert was unreachable regardless: `operational_health_check` called
`project_disk()` **bare**, so `database_bytes` defaulted to 0. A zero numerator
against a 200 GB target meant the alert could only fire if the box grew
6.7 GB/day while reporting no usage at all. It had never fired and could not.

Where the space actually goes — measured per volume, and the largest consumer
is not ours:

```
twitter-saas_media_data      34 GB      portfolio-saas_pgdata     20 GB
bama-saas_postgres_data     3.1 GB      /var/backups/portfolio    16 GB
docker build cache          6.8 GB      dangling volumes         3.6 GB
```

Fixed: `project_disk` now statvfs's the device (the container's `/` is the host
data device through overlay2, verified), takes the *faster* of the device growth
series and our own as the conservative estimate, and reports the breakdown
separately from the headline. `VPS_DISK_BUDGET_GB` is deleted from settings and
compose. The Ops meter draws the device rather than our share. Five regression
tests, including one pinning that the alert is reachable.

`deploy.sh` also refuses to start a deploy under 10 GB free — a deploy writes a
~1.7 GB dump and then builds images, and running out midway truncates the day's
restore point — and prunes build cache older than a week after the health checks
pass (derived data; worst case is a slower build).

**Partly fixed.** 2.58 GB reclaimed from Docker build cache, taking the device
from 78% to 76% (110 GB → 108 GB used, 32 GB → 34 GB free). The window is
measured, not chosen: this box deploys most days, so `until=168h` reclaimed
**0 B** — every cached layer was younger than the filter. `until=72h` freed
1.35 GB and a further `until=48h` freed 1.23 GB, so `deploy.sh` now uses 48h.

**Not fixed, and needing your decision.** The remaining levers are
infrastructure. The dangling volumes, now identified rather than guessed:

| volume | size | created | contents |
|---|---|---|---|
| `5de2825f8fcb…` | 3.0 GB | 2026-09-07 | a PostgreSQL data directory |
| `f51c4e3d4f0b…` | 62 MB | 2026-09-07 | a PostgreSQL data directory |
| `portfolio-saas_mongodata` | 322 MB | 2026-08-19 | WiredTiger — MongoDB |
| two others | 8 KB each | — | empty |

Only the Mongo one is unambiguously ours and unambiguously dead: nothing in the
stack has used MongoDB since it was removed. The two Postgres directories are
anonymous volumes from a container started without a named mount, and they could
belong to a neighbouring stack — which is exactly why I have not deleted any of
them. ~3.4 GB, one authorization away.

Beyond that: a neighbouring stack's `twitter-saas_media_data` is 34 GB, 23% of
the whole device and the single largest consumer, or grow the disk.

### P1 CONFIRMED: every tick query by Jalali date scanned all 1,240 chunks

The tick hypertable is range-partitioned on `ts` because TimescaleDB cannot
partition a varchar, while the domain key beside it is the Jalali `date` string.
A predicate on `date` alone carries no partition information, so the planner
cannot exclude a single chunk. Measured on production, 2026-09-09, against 58M
ticks in 1,240 chunks — same 111 rows returned either way:

```
WHERE symbol = ? AND date = ?                     2,133 ms cold    318 ms warm
WHERE symbol = ? AND date = ? AND ts >= ? < ?        37 ms cold    2.2 ms warm
```

`EXPLAIN` on the delete confirms the shape: 1,220 sequential chunk scans against
19 index scans. The tick re-ingest path deletes a symbol-day before rewriting it,
so that scan was paid on **every replace**. It also explains the identical
`seq_scan = 139` on every `_hyper_1_*` chunk in `pg_stat_user_tables` — the
signature of whole-hypertable scans — and the 1.5 billion tuples read from
`marketdata_marketcandle`.

Fixed with one helper, `jalali.ts_window`, applied at the two sites with a
bounded day set: the ingest delete and the archive tick verifier.
`_tick_dates_stored` is deliberately left alone — "which days do we hold?" is
genuinely a whole-history question and cannot be pruned.

The window is padded a day on each side because it only has to be a *superset*;
the failure that matters is a delete that misses rows, which is how the 3.7M
duplicate candles were made. Padding still prunes 1,240 chunks to 3. Five tests
pin the superset property, including one that the historical 7-hour derivation
skew stays inside the window and one that a neighbouring day is not swept up.

Honest caveat, measured: chunk exclusion is a plan-time optimisation, so a
*generic* plan loses it. The same parameterized query ran 2 ms on its custom
plans and 3,365 ms on the sixth execution, where PostgreSQL switched to a generic
plan and then reverted. Worse than the literal best case, still two orders of
magnitude better than no window.

### P2: 767 MB of index that has never been read

`marketdata_stocktransactiontick_id_idx` — a plain, non-unique index on `id`
across 1,240 chunks — has `idx_scan = 0`, and `pg_stat_database.stats_reset` is
NULL, so that is lifetime, not a window. Uniqueness is enforced by
`uniq_stock_tick_symbol_date_row_time`; nothing looks a tick up by id. On
`marketdata_workflowrun` another 225 MB is unread, including both `task_id`
indexes — the only query touching that column is `Q(task_id__icontains=...)` in
`admin_api.py`, and a leading-wildcard `LIKE` cannot use a btree or a
`varchar_pattern_ops` index, which is exactly why the counter says zero.

**Not done, deliberately.** Roughly 1 GB and a materially cheaper insert path on
the two hottest write tables, but it is a schema migration against a production
hypertable and the evidence is a counter rather than a proof. It wants its own
change with a rollback plan, not a tail-end addition to this batch.

### Authorization: swept, and no IDOR found

A verified negative rather than an assumption. All 15 account-scoped routes in
`portfolio/urls.py` carry `account_id` (or address the account itself), so
ownership is decidable from the URL alone. As a second authenticated user
against a fully populated foreign account — account, holding, liability, ledger
entry — **25 assertions, all correct**:

- 19 object/action routes answer **404**, not 403: telling an intruder that an
  account exists but is not theirs is itself a disclosure.
- 2 list routes answer 200 with an **empty page** (they filter `account__user`
  rather than resolving the account first). No rows leak, and it is not an
  existence oracle — the same empty page comes back whether or not the id
  exists.
- 3 create routes answer 404 **with payloads valid enough to reach the view
  body**, and no row appears in any table. A 400 from serializer validation
  would have proved nothing about ownership, since the serializer runs first.

The sweep is guarded: `test_every_account_scoped_route_is_in_the_isolation_sweep`
reads `urlpatterns` and fails when a route under `accounts/<id>/` is not covered.
That guard was observed failing correctly, naming the three routes it did not yet
cover, before they were added. Without it the sweep would only ever prove things
about routes somebody remembered to list.

DRF's `DEFAULT_PERMISSION_CLASSES` is `IsAuthenticated`, so the handful of views
carrying no explicit `permission_classes` are closed by default, and every admin
surface declares `IsAdminUser` explicitly.

### Mail is an explicit degraded state

Configuration is checked before a deploy mutates production and reachability is
checked afterward. Both warn instead of masking the problem. A superuser can
issue the same single-use reset link out of band, so accounts remain recoverable
while self-service delivery is degraded; a real relay is still required to
remove that operator step.

## Critical journeys

| Journey | Status |
|---|---|
| Sign in | e2e passed; `last_login` stamped on JWT obtain |
| Forgot / reset password | browser-verified; SMTP still required for prod |
| History reverse vs delete | live book hides reversed pairs (**decision**, below) |
| Ops People and books | last sign-in populates after a real login |
| Payments | N/A — no payment product exists |

## Findings (priority)

### P0 — release blockers

1. Password recovery closed in code and browser-verified.
2. **SMTP must be configured in production.** Deployment config, not code.

### P1 — fixed in batch 5

3. **`logger` was undefined in `portfolio/views/analytics.py`.** Both uses sat
   inside `except` handlers written to degrade gracefully — one when the broker
   refuses a background refresh, one when the snapshot write fails — so a broker
   outage turned a servable cached MyOptimal page into a `NameError` 500.
   Fixed; regression test pins it (verified failing without the fix).
4. **Refresh-token tables grew without bound.** `ROTATE_REFRESH_TOKENS` +
   `BLACKLIST_AFTER_ROTATION` write an `OutstandingToken` row on every refresh
   and nothing ever removed them. Added
   `portfolio.tasks.prune_expired_refresh_tokens` on the 02:10 beat slot (no
   enable flag: it deletes credentials the auth layer already refuses).
5. **`_revoke_all` was an N+1 in the request path.** One SELECT + one INSERT per
   token, over every token a user had ever held, inside password-change and
   account-delete. Now one `bulk_create(ignore_conflicts=True)` over unexpired
   tokens only; pinned at 2 queries by `django_assert_num_queries`.
6. **Two tests monkeypatched `marketdata.admin_api.get_quota_status`,** a name
   that module never called (the real consumer is `admin_telemetry`). The patch
   had always been a no-op; removed.

### P1 — standing

7. Freshness stamps. **Done.**
8. Warehouse coverage staff-only. **Done.**
9. History is the **live book**: reversed pairs are omitted. The correction
   stays in the database. Showing audit rows is a product expansion, not a bug.
10. JWT obtain stamps `User.last_login`. Cookie refresh and failed login do not.

### P2 — fixed in batch 5

11. **No linter existed for either language.** Added `ruff` (backend,
    correctness rules only) and `eslint` (frontend, `no-undef` first), both
    wired into CI. ESLint exists because Rollup resolves module specifiers, not
    identifiers: a missing import builds clean and throws `ReferenceError` in
    the browser. Both trees are clean at introduction.
12. **Ops meter had no accessible name.** `InfraMeter`'s progressbar announced
    "progressbar, 62" with nothing saying what was 62% full. Added `aria-label`
    and `aria-valuetext` on both meters, so the announced value is the figure
    sighted users see ("18.4 GB of 250 GB") rather than a bare percentage.
13. **Four memos were defeated by their own dependencies.** `x?.y || {}` mints a
    fresh object every render, so `Ops` rebuilt both attention tables, and
    `Comparison`/`AddTransactionDialog` re-ran their effects, on every render.
14. Dead code removed: 5 uncalled `archive.py` helpers (caller-traced in batch
    4), 6 uncalled accessors across `endpoints`/`market_state`/`live_states`/
    `returns`, ~90 unused or duplicated imports, and a stale Recharts CSS rule
    for a library the app no longer uses.
15. **Ops badge contrast: measured, fixed, guarded.** Every variant tinted its
    own background (`bg-[var(--c-good)]/10`), which composites into the surface
    the label is measured against. The text twins clear AA on a plain surface by
    only 4.61:1, so there was nothing to spend: **9 of the 24 variant/surface
    pairs fell under 4.5:1**, worst light-mode critical on panel-2 at 4.04:1.
    No opacity rescues it — 3% still only reaches 4.42:1. Dropping the tint
    alone then left the chip with no visible edge (the 40% display-colour border
    is 1.56:1 for `good` in light mode, against WCAG 1.4.11's 3:1). Both halves
    are fixed by drawing the border in the text twin at full opacity, which is
    AA against every surface by construction. `contrast.test.mjs` now pins the
    absence of a tint and the border's 3:1.

    Then the guard was generalised from Badge to the whole tree, and found the
    same defect in **six more places** — including the **sign-in error banner**
    (4.04:1 in light mode), the registration-closed and reset-sent banners, the
    legal draft notice, the danger `Button`'s hover and the account menu's
    danger row. All fixed the same way. The rule is narrow on purpose: a tint
    only fails under a `-text` twin. `ErrorState`, the Dashboard warehouse
    warning and the logo hover tint too and all pass at 11–13:1, because their
    text is plain `--c-text`; flagging those would force a change that buys
    nothing. Verified by reintroducing a tint and watching the test name the
    file and line.
16. **The SPA shell had no `Cache-Control`.** `index.html` is the one file whose
    name survives a deploy, and heuristic freshness can keep serving the
    previous one — which names asset hashes the new deploy no longer has.
    `no-cache` added in `nginx.conf` (with the security-header snippet
    re-included, per the `add_header` inheritance trap), asserted in `lhci.sh`.
17. **Public API surface is now pinned.** 10 of 95 routes are reachable without
    a JWT, each for a stated reason; `test_only_the_named_routes_are_reachable_
    without_a_token` walks the resolver and fails if that set changes. DRF's
    default is `IsAuthenticated`, so a route goes public via one copied line
    that looks exactly like the intentional ones.
18. **Vitest was flaky under load.** testing-library polls `waitFor` for 1000 ms
    of wall clock; under CPU contention the failure landed on whichever spec was
    scheduled unluckily (observed: Dashboard on one run, useApi on the next,
    no change in between). `asyncUtilTimeout: 5000` in `test-setup.js`. Three
    consecutive clean runs since.

## Decisions (keep)

- Password reset always returns the same 200 whether the email exists.
- Do not auto-sign-in after reset; revoke every refresh token.
- Do not build email verification until memberships reopen.
- History **hides** reversed pairs. Do not add an audit-trail UI unless asked.
- Cookie refresh is not a new sign-in.
- `marketdata/sources/tsetmc_direct.py` keeps its two uncalled fetchers
  (`fetch_best_limits`, `fetch_intraday_trades`). They are a documented, gated
  integration surface waiting on `IRAN_EGRESS_PROXY`, with a management command
  that probes them — not accidental dead code.
- Ruff does **not** enable F841 (assigned but never used): a test creating a
  database row for its side effect is the fixture, not a mistake.
- ESLint does **not** take `eslint-plugin-react-hooks`' v7 recommended set.
  `set-state-in-effect` and `refs` flagged 12 places in working code and would
  each need a refactor; the config takes `rules-of-hooks` and
  `exhaustive-deps` instead.

## Measured and deliberately NOT changed

**Splitting the `charts` chunk is not worth it.** It is the largest thing a user
downloads (642.9 kB raw / 214.5 kB gzip) and it is on the Dashboard's path, so
it looked like the obvious win. Measured by rebuilding with only `LineChart`
registered: the chunk drops to 550.2 kB / 184.1 kB. So all four extra chart
types plus `VisualMapComponent` cost **92.7 kB raw, 30.5 kB gzip** — 14% — and
the other 86% is echarts core plus the canvas renderer, which every page needs.
Splitting would buy 30 kB gzip in exchange for lazy boundaries across every
chart call site. Revisit only if echarts core itself is replaced.

**No AbortController in `useApi`.** Abandoned requests are not cancelled when
deps change. Client-side abort would not fix the visible symptom either: the
`concurrency_cap` slot is held by the *server* until the sync view returns, so
an aborted analytics request still occupies one of the user's two slots.
Production serves HTTP/2, so the browser-side connection cost is small. Left
alone rather than plumbing a signal through ~50 call sites for a benefit that
cannot be measured from here.

## Next unfinished step

1. **Deploy the uncommitted `EMAIL_*` block, then configure a real relay.**
   Until both happen, password reset is silently dead for every user. This is
   the only remaining P0. It is a **confirmed external blocker**: there is no
   MTA and no SMTP credential anywhere on the VPS, and a self-hosted relay on a
   bare IP would not deliver — it needs a transactional-mail account only the
   owner can open. Everything on this side is ready: settings block written,
   compose plumbing verified, Ops console probes the socket, `deploy.sh` warns
   on every deploy until it works.
2. ~~Decide the Caddyfile change.~~ **Done** — Portfolio and Bama now serve
   `script-src 'self'`; newsintel keeps `'unsafe-inline'` on its own snippet
   because Next.js requires it.
3. Recreate `portfolio-saas/.venv` from the pinned requirements so local runs
   stop disagreeing with CI.
4. **Container-level header checks completed during recovery**, including
   `cache-control: no-cache`, against an isolated nginx container on the VPS.
   A fresh multi-stage image build and full `lhci.sh` run remain CI checks.
5. Playwright e2e was **deliberately not run**, confirmed with the operator.
   The only environment available is production, whose accounts 13/14 hold a
   real family portfolio, and the specs write transactions and ledger entries.
   Needs a staging stack before it can be a routine check.

## Edge change made on the VPS (authorized, applied, verified)

`/opt/apps/vps-edge/Caddyfile` is shared infrastructure and is not shipped from
this repository. The change applied there is recorded above; rollback is
`cp Caddyfile.bak-precsp-20260908-211303 Caddyfile && docker exec vps-edge-edge-1
caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile`.

For reference, the Portfolio-only alternative that was *not* taken (it would
have left Bama on `'unsafe-inline'` needlessly):

```caddyfile
# Portfolio ships its own CSP from frontend/security-headers.conf and needs no
# inline scripts: one external module script, no inline bodies, no on* handlers.
(security_headers_portfolio) {
	header {
		Content-Security-Policy "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self' https://*.ingest.sentry.io"
		Permissions-Policy "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
		Referrer-Policy "same-origin"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		-Server
	}
}
```

A Caddyfile syntax error takes every site down until reverted, so any edit here
wants a backup and `caddy validate` before `caddy reload`. That is the procedure
that was followed.

## Launch prerequisites (exact)

1. Set a 50+ character `DJANGO_SECRET_KEY` (already enforced when `DEBUG=0`).
2. Set `REDIS_URL` (already enforced when `DEBUG=0`).
3. **Configure SMTP** (`EMAIL_HOST` and friends) or password reset is silent.
4. Set `FRONTEND_PASSWORD_RESET_URL` to the public origin + `/reset-password`.
5. Rotate any API keys that have been in chat/logs (`BRS_API_KEY`, `TSETMC_API_KEY`).
6. VPS GitHub secrets (`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_KNOWN_HOSTS`).
7. Backup passphrase file readable by the deploy user.
8. Confirm `REGISTRATION_OPEN` policy with the operator before inviting users.
9. Sentry / alert webhook optional but recommended.
10. Do not enable live payment charges (none exist).

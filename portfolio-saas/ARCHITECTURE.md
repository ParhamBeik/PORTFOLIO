# Architecture

Where things live and which rules must not be broken. Domain invariants — unit
policy, warehouse safeguards, the archive's semantics — live in
[`CLAUDE.md`](CLAUDE.md) and [`docs/REFERENCE.md`](docs/REFERENCE.md); this file
is about structure only.

## Layers

Dependencies flow one way. The module-level import graph is acyclic and must
stay that way.

```
transport            config/urls.py · portfolio/urls.py · accounts/urls.py
                     portfolio/views/* · accounts/views.py · marketdata/admin_api.py
                     */management/commands/*  ·  frontend/src/pages/*
   |                 thin: parse, authorize, delegate, serialize. no business rules.
   v
domain               portfolio/services/*  ·  marketdata/{archive,quota,calendars,
                     integrity,validation,universe,coverage_report}.py
   |                 the actual arithmetic. no request objects, no serializers.
   v
data access          portfolio/models.py · marketdata/models.py · accounts/models.py
                     portfolio/live/*  ·  marketdata/{fetchers,sources/*,ingest}.py
   |
   v
infrastructure       PostgreSQL/TimescaleDB · Redis · Celery · MinIO/S3 · BrsApi
```

Two bounded contexts. `portfolio` owns user-scoped data (every table has a user
or account FK). `marketdata` owns the symbol-keyed market warehouse and has no
user FKs at all. `portfolio` may read `marketdata`; `marketdata` must not reach
into user data except through the narrow catalog seam.

## Directory map

```
portfolio-saas/
├── docker-compose.yml          dev     (Vite + gunicorn/uvicorn, ports published)
├── docker-compose.prod.yml     prod    (standalone; frontend joins vps-edge, no ports)
├── scripts/                    deploy, backup, restore, ops cron, egress setup
├── docs/README.md              what each doc is and whether it is still live
├── backend/
│   ├── config/                 Django project: settings, urls, celery, health,
│   │                           api.py (JSON + error translation), observability.py
│   ├── accounts/               User model + JWT auth
│   ├── portfolio/              user-portfolio domain
│   │   ├── models.py           Asset, Account, Holding, Price, Snapshot, LedgerEntry
│   │   ├── services/           valuation, ledger, performance, returns, diagnostics,
│   │   │                       optimization, comparison, insights, timeline, …
│   │   ├── views/              catalog · ledger · valuation · analytics · admin_ops
│   │   │                       (+ _common for what two or more of them share)
│   │   ├── live/               price loop: fetcher, extractor, redis_client
│   │   └── tasks.py            Celery heartbeat + pruning + snapshot jobs
│   ├── marketdata/             market-history warehouse (separate bounded context)
│   │   ├── endpoints.py        the endpoint registry everything validates against
│   │   ├── fetchers.py         BrsApi clients        sources/  direct origins
│   │   ├── ingest.py           payload -> rows       tasks.py  the sync schedule
│   │   ├── archive.py+quota.py gap-driven backfill under a request budget
│   │   ├── calendars.py        which days a market was actually open
│   │   └── admin_api.py        staff-only /api/admin/* Ops console backend
│   ├── perf/                   request/page latency rollups (middleware, client beacon,
│   │                           hourly table, `perf_report`); see docs/PERFORMANCE.md
│   └── tests/                  thematic suites, one per bounded concern
└── frontend/src/               pages/ · components/ui.jsx + charts.jsx
                                api.js + useApi.js (the one fetch pattern)
```

## Rules a contributor must not break

**1. Import at module scope. Defer only for a reason you can name.**
The module-level graph is acyclic, and the file header is where the next reader
learns what a module depends on. A function-local import is allowed only when it
is one of these, and the line should say which:

- it would close an import cycle (six of these exist; each is commented);
- the target transitively loads the scientific stack (`cvxpy`/`scs`/`numpy`/
  `sklearn`) and this process must not pay for it — the live price worker never
  solves anything, and `scs`'s bundled OpenBLAS SIGILLs on the deployed vCPU;
- it sits inside a `try`/`if` where the condition is the point;
- something rebinds the name at runtime. This one is subtle: a function-local
  import resolves the attribute on the module object at **call** time, so
  `patch("marketdata.fetchers.fetch_derivatives")` reaches it and a module-level
  import binds once and sails past. Moving such an import silently changes what
  the call site gets.

**2. A view belongs to the module its URL prefix belongs to.**
`portfolio/views/` is a package split along the groupings `urls.py` already
used. Import from the concern module (`from .views.ledger import TradeView`) —
the package root deliberately exports nothing, so every view has exactly one
import path. A shared helper goes in `_common` only once a second module needs it.

**3. Migrations are append-only.** Never edit, delete, reorder or re-squash one
that has run in production. Each app has one `0001_squashed` carrying
`replaces=`; marketdata adds `0002_storage_tuning` for the autovacuum triggers
and the TimescaleDB tick hypertable. Schema changes ship expand → migrate →
contract across separate deploys. Migrations apply automatically on every deploy.

**4. Production publishes no ports.** `docker-compose.prod.yml` is standalone,
not an override of the dev file, and that is deliberate: the dev file publishes
Postgres, Redis, the API and Vite to the host, and Compose merges `ports` by
concatenation with no way for an override to drop an inherited binding. Layering
the two would publish the database on a VPS shared with three other stacks. The
duplication between the files is the price of that guarantee — see
[`docs/history/REFACTOR_REPORT.md`](docs/history/REFACTOR_REPORT.md).

**5. The frontend has one of everything.** Pages compose `ui.jsx` primitives and
write no bespoke panel/loading/error markup; charts go through `charts.jsx`
wrappers and never `import echarts` or a hex colour directly; data fetching goes
through `useApi`. Every element a test or screen reader needs takes
`testId` → `data-testid="<page>-<thing>"`.

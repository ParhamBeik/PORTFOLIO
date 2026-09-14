# Phase 0 Recon + Refactor Plan — portfolio-saas

## Context

The mission brief asks for dramatic simplification of a live, auto-deploying codebase.
Phase 0 recon is complete and the honest finding is that **this repository is already at or
near the target architecture the brief describes**. The premise of the brief — sprawl,
duplication, dead code, config chaos — mostly does not hold here. Measured:

| Probe | Result |
|---|---|
| Tracked files / lines | 301 files, ~87k lines (roughly half are tests) |
| Backend modules with zero inbound references | **0** (only tests, management commands and migrations, all dynamically discovered) |
| `ruff --select F,E9,ERA` findings | 1 real (`F541`), 1 real (`F841`), 3 `ERA001` of which 2 are test scaffolding and 1 is a false positive on a section divider |
| eslint across `frontend/` | clean |
| Top-level import cycles | **0** |
| Cross-file duplicate blocks (8-line windows) | 2 clusters, **both intentional** (frozen parity oracle; a deliberately different tone-coloured column) |
| Secrets ever committed | **none** — `git log --all` over `*.env*`, `*secret*`, `*.pem`, `*.key` returns only `.env*.example` files |
| Unused frontend exports | 6, all used inside their own file (only the `export` keyword is surplus) |

So this is **not** a demolition job. The real work is a short list of evidence-backed
consolidations plus one large mechanical legibility change. Scope was confirmed with the
user; where I flagged risk and the user chose the riskier option, that decision is recorded
inline below and the batch carries an extra mechanical verification gate instead.

---

## Stack summary

Django 5.2 + DRF 3.18 (class-based `APIView`s registered through explicit `urls.py`
urlpatterns — no viewsets, no routers), PostgreSQL 16 + TimescaleDB, Redis, Celery 5.6
(three services: `live`, `archive`, `beat`), served by gunicorn/uvicorn in Docker Compose.
Frontend is React 19 + Vite 8 + Tailwind 4 + echarts 6, plain JS (no TypeScript), tested
with `node --test` + vitest + Playwright. Python 3.11, Node 22, both pinned in CI and in the
Dockerfiles. `package-lock.json` is committed; Python is pinned exactly in `requirements.txt`.

## Deployment reality (what constrains every batch)

- **Pipeline** `.github/workflows/ci-cd.yml`: jobs `backend` (ruff → `makemigrations --check`
  → pytest → shellcheck → DB restore drill), `frontend` (eslint → `test:unit` → vitest →
  build → Lighthouse against the real prod image), `audit` (deliberately not gating),
  `deploy-configured`, `deploy`. `deploy` needs `backend` + `frontend` green.
- **Deploy** = ssh to VPS → `git reset --hard origin/main` → `scripts/deploy.sh`: disk-space
  precheck, encrypted pg dump, build, `run --rm migrate`, `up -d`, `check --deploy`,
  `migrate --check`, `/api/health/ready/` over HTTPS, `celery inspect ping` on both workers,
  and a beat *uptime* check. **Migrations apply automatically on every deploy.**
- **No staging, no canary.** `scripts/run_staging_migration_check.sh` exists but is a manual
  migration rehearsal, not an environment. This is why batches stay small.
- **Rollback:** `git revert <sha> && git push` → CI redeploys the previous tree. There is no
  image-tag rollback; forward-revert is the only mechanism.
- **String-reachable entrypoints** (invisible to static analysis, never delete):
  25 beat entries in `config/celery.py` naming tasks by dotted path; `task_routes` mapping
  those to the `live`/`archive` queues; 23 management commands under `*/management/commands/`;
  `INSTALLED_APPS`/`MIDDLEWARE`/`REST_FRAMEWORK` dotted paths in `config/settings.py`;
  `marketdata/templates/admin/index.html`; the on-VPS cron installed by
  `scripts/install_ops_cron.sh` (`ops_check.sh`, `watchdog_prices.sh`, `backup_postgres.sh`).

## Current architecture, and where it deviates

Layering is already correct and *enforced by measurement*: the module-level import graph is
acyclic, `config` → apps → services → models flows one way, and `marketdata` is a genuinely
separate bounded context with no user FKs. Three real deviations:

1. **479 deferred (function-local) imports**, 338 of them intra-project. Only **6** guard a
   true cycle (`marketdata.archive→tasks` ×2, `portfolio.models→tasks` ×2,
   `marketdata.ingest→tasks`, `portfolio.services.returns→marketdata.universe`). The other
   332 are habit. The acyclic top-level graph is therefore partly an artefact: the real
   dependency edges are hidden inside function bodies, which is precisely the "find the logic
   in 60 seconds" failure the brief targets.
2. **A 123-line re-export barrel** at `portfolio/views/__init__.py`, kept only so `urls.py`
   sees the pre-split surface, plus a 161-line test (`tests/test_views_package.py`) whose job
   is to police the barrel. Two real importers.
3. **Point-in-time reports carried as documentation**: `docs/` holds 2,143 lines of dated
   evaluations and `work/` holds 363 lines of finished-audit scratch at the repo root, which
   nothing references.

Plus two small factual drifts: `requirements.txt` comments cite `pricing/optimization.py`,
`pricing/returns.py` and `marketdata/codal_extract.py` — none of which exist — and the
README's dev quick-start says "create `.env`" with no `.env.example` to copy from (only
`.env.production.example` survives).

## Target architecture

Unchanged from today's, which is already the conventional Django-5/DRF layout: per-app
packages, explicit `urls.py`, business logic in `services/`, thin `APIView`s, one squashed
migration per app. The target adds only: dependencies declared at module scope, no re-export
barrels, `docs/` holding durable reference rather than dated reports, and a committed
`ARCHITECTURE.md`. **No new patterns, no new directories, no new dependencies.**

---

## Batches — ordered lowest-risk first

Each is one commit under the §2 Landing Protocol: pull, change one category, verify, review
the diff, commit with WHAT+WHY, push, watch the deploy and the logs before starting the next.

### B1 — Delete finished-audit scratch (`work/`)
9 files, 363 lines, zero inbound references anywhere in the tree.
**Breaks if wrong:** nothing; these are prose. **Verify:** repo-wide grep for each filename → 0 hits.

### B2 — Prune and consolidate `docs/`
Delete `QUOTA-USAGE-REPORT-2026-09-04.md` (dated report, superseded by the Ops console).
Fold anything still true out of `PRODUCT-EVALUATION.md` / `PRODUCTION-READINESS.md` /
`LIGHTHOUSE.md` into `docs/REFERENCE.md`, then delete the three reports. Keep
`DATA-SOURCES.md` and `REFERENCE.md` — both are referenced and both are current.
**Verify:** grep every deleted filename across `*.md`, `*.py`, `*.yml`, `*.sh`, `*.jsx` → 0 hits;
fix the two intra-doc links in `PRODUCT-EVALUATION.md`'s successor text.

### B3 — Fix factual drift in comments and add `.env.example`
Correct the three dead paths in `requirements.txt` comments to
`portfolio/services/optimization.py`, `portfolio/services/returns.py`, `marketdata/codal_pipeline.py`.
Add `portfolio-saas/.env.example` listing every variable `config/settings.py` reads, with
descriptions and safe placeholders, so the README's quick-start is executable.
Fix `F541` (`backfill_validation.py:197`) and the unused `now` (`portfolio/tasks.py:535`).
**Verify:** ruff clean; `docker compose up` from a fresh `.env` copied from the example boots.

### B4 — Drop the six surplus `export` keywords
`hasSessionHint` (api.js), `COVERAGE_COLORS` / `moneyTrendDomain` / `shareTrendDomain`
(charts.jsx), `DISABLED_DIM` (ui.jsx), `QUANTITY_MAX` (quantity.js) — each is referenced only
inside its own module. Remove `export`, keep the binding.
**Verify:** eslint, vitest, `node --test`, `npm run build`, repo-wide grep per identifier.
*(Confirm first that no Playwright spec or test reaches for them by name.)*

### B5 — Collapse the `portfolio/views/` re-export barrel
Point `portfolio/urls.py` and `config/urls.py` at the concrete submodules
(`from .views.catalog import AccountListCreateView`, …). Delete the 123-line `__init__.py`
body, leaving the package docstring. Delete `tests/test_views_package.py`, which exists only
to pin the barrel, **except** its route-coverage assertion — move that into
`tests/test_serving_resilience.py` if it asserts anything about URLs rather than about the
barrel. Update the two test files that import `from portfolio.views import …`.
**Breaks if wrong:** every portfolio endpoint 404s or the app fails to import — caught by the
suite and by `/api/health/ready/`. **Verify:** full pytest, ASGI boot, `manage.py show_urls`-
equivalent diff of the resolved urlpatterns before and after.

### B6–B17 — Hoist deferred imports, one module per commit
Order by count, largest first: `marketdata/tasks.py` (62), `portfolio/services/valuation.py`
(42), `portfolio/views/analytics.py` (27), `portfolio/services/ledger.py` (23),
`portfolio/tasks.py` (22), `portfolio/services/returns.py` (17), `portfolio/views/valuation.py`
(13), `marketdata/archive.py` (12), `portfolio/live/fetcher.py` (10), `marketdata/calendars.py`
(9), `marketdata/admin_api.py` (6), `portfolio/services/optimization.py` (6).

**Leave the 6 genuine cycle guards in place**, each with a one-line comment naming the cycle
it breaks. Also leave in place, and annotate rather than hoist, any deferred import that is:
a heavy scientific import (`scipy`, `sklearn`, `cvxpy`, `PyPortfolioOpt`) deliberately kept
out of the web worker's import path; or an import inside a module that Django loads during
app-registry construction, where hoisting risks `AppRegistryNotReady`. I will classify each
import against those two rules before moving it and report the count I declined to move.

**Verify, per batch:** ruff; full pytest; `python -c "import config.asgi"` under
`DJANGO_SETTINGS_MODULE=config.settings`; `celery -A config inspect registered` listing all
27 tasks; and for the two `tasks.py` modules, a diff of the beat schedule's resolved task
names. Watch worker memory and boot time on the deploy after each of the two `tasks.py` batches.

> Concern, stated once: this is 332 mechanical edits across the hottest modules in the
> system, and its failure mode (`AppRegistryNotReady`, or a circular import that only
> manifests under the Celery worker's import order, not pytest's) is a boot failure rather
> than a test failure. The per-module batching, the ASGI-boot check and the Celery
> `inspect registered` check exist specifically to catch that before it reaches the VPS.

### B18 — Consolidate the compose files into base + override
`docker-compose.yml` and `docker-compose.prod.yml` share 58% of their non-comment lines
(160 of prod's 275). Restructure as `docker-compose.yml` (base) + `docker-compose.prod.yml`
(override only), and update `scripts/deploy.sh`'s `compose=(…)` array to pass both `-f` flags.

**This is the one batch that touches the deploy path, and there is no staging environment to
rehearse it against.** I recommended leaving it alone; the user chose to consolidate, so the
batch carries a mechanical equivalence gate instead of a judgement call:

1. Capture `docker compose -f docker-compose.yml config` and
   `docker compose -f docker-compose.prod.yml --env-file .env.production.example config`
   **before** the change.
2. Make the change.
3. Capture both **after**, with the new `-f` flag sets, and require the rendered YAML to be
   **byte-identical** (after key sorting). Compose's `config` output is the fully-resolved
   spec the daemon acts on, so an identical render is proof of zero behaviour change —
   stronger evidence than any test.
4. Ship it as its own commit with nothing else in it, and confirm `deploy.sh`'s beat-uptime
   and both `celery inspect ping` checks pass on the deploy before continuing.

If step 3 does not come out identical, I stop and report rather than reconciling by hand.

### B19 — `ARCHITECTURE.md`
Layer diagram, directory map, and the small set of rules a contributor must not break
(deferred imports only for the six named cycles; a view belongs to its URL prefix's module;
warehouse writes are verbatim, conversion lives only in `currency.to_toman()`; migrations are
append-only). Most of this already exists as prose in `CLAUDE.md` — this extracts the
structural half into a file a contributor will actually find, and `CLAUDE.md` keeps the
domain invariants.

### B20 — `REFACTOR_REPORT.md`
Final report per §7.

---

## Deletion candidates

| Path | Evidence | Confidence | If wrong |
|---|---|---|---|
| `work/` (9 files) | zero inbound refs, repo-wide; finished-audit scratch | high | nothing, prose only |
| `docs/QUOTA-USAGE-REPORT-2026-09-04.md` | dated, no inbound refs | high | lose a historical snapshot (git keeps it) |
| `docs/PRODUCT-EVALUATION.md`, `PRODUCTION-READINESS.md`, `LIGHTHOUSE.md` | referenced only by each other | medium | lose reasoning; mitigated by folding live conclusions into `REFERENCE.md` first |
| `portfolio/views/__init__.py` re-export body | 2 real importers, both rewritten in the same commit | high | every portfolio endpoint 404s; caught by suite + health check |
| `tests/test_views_package.py` | tests only the barrel B5 removes | medium | lose route-coverage assertion; mitigated by relocating it |
| 6 `export` keywords (frontend) | referenced only within their own module | high | `ReferenceError` in the browser; caught by eslint `no-undef` |

## Suspected dead — needs runtime confirmation (not deleting this pass)

- `marketdata/burst_probes.py` — reached only via a beat entry and a deferred import from
  `config.celery`. Alive by configuration; confirm it actually claims probes in production
  logs before anyone considers it dormant.
- `portfolio/management/commands/clean_mispriced_data.py` and
  `marketdata/management/commands/clean_invalid_candles.py` — one-shot repair commands, last
  touched 2026-07-24 and 2026-08-08. Management commands are string-reachable; deleting them
  needs a human decision, not static evidence.
- `scripts/setup_iran_egress.sh` (226 lines) — infrastructure bootstrap, off-limits under §5.

## Bugs / drift found, not fixed (reported per §0.1)

- `requirements.txt` comments name three files that do not exist (`pricing/optimization.py`,
  `pricing/returns.py`, `marketdata/codal_extract.py`). Comment-only; corrected in B3.
- README's dev quick-start instructs `create .env` but `portfolio-saas/.env.example` was
  deleted at some point; only `.env.production.example` remains. Fixed in B3.
- `portfolio/tasks.py:535` assigns `now` and never uses it — dead local, not a behaviour bug.

## Deliberately left alone

- **`frontend/src/pages/Ops.jsx` (2,389 lines) and `Dashboard.jsx` (1,716).** Large but
  self-contained, correct, and not duplicated elsewhere. Splitting them is taste-driven churn
  on the two pages with the most e2e coverage riding on their DOM. §6.
- **The `Dashboard`/`Family` near-duplicate performance table.** They differ intentionally —
  `Family` tone-colours the percentage, `Dashboard` does not. This is the exact §6 trap.
- **`tests/legacy_oracle/`.** Duplicates `live/extractor.py` by design; it is the frozen
  parity oracle the pricing path is checked against.
- **Auth, throttling, CORS, CSP, `security-headers.conf`, `nginx.conf`, the CI workflow,
  `scripts/*.sh` other than the one line B18 must change, and every migration.** §5.
- **`.claude/` / `.codex/` / `.agents/` config.** `.agents/skills/new-chart/SKILL.md` and
  `.claude/skills/new-chart/SKILL.md` are byte-identical duplicates, but they are two tools'
  required locations, not a codebase concern.

## Open questions

1. `docs/PRODUCT-EVALUATION.md` carries a prioritised A–F roadmap. Is any of it still live
   work? If so I will move that table into an issue or `README` rather than delete it with
   the rest of the document in B2.
2. B18 changes `scripts/deploy.sh`. That file is on the §5 list. I am treating the user's
   choice to consolidate the compose files as approval for that one-line change specifically,
   and nothing else in the script.

## Verification, end to end

Per batch: `ruff check .` → `python -m pytest -q` (needs local Postgres; the repo's own note
says the checked-in `.venv` is stale, so a green local run is corroboration, not proof — CI is
the gate) → `npm run lint && npm run test:unit && npm run test:vitest && npm run build` →
`docker compose up` and hit `/api/health/ready/`, `/api/valuation/` and one rendered page →
repo-wide grep for every removed identifier and filename → `git diff --stat` then `git diff`
read line by line → push → watch the pipeline, then `curl https://<domain>/api/health/ready/`
and `docker compose -f docker-compose.prod.yml logs --since` the deploy for new 5xx or import
errors. Any failure: `git revert` and push before diagnosing.

## Status

Phase 0 complete. This file is the Phase 0 deliverable and lands on its own commit.
Work stops here pending approval of the batch list; B1 does not start until that approval
arrives.

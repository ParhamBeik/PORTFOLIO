# Refactor report

Twenty commits, `71612d1..HEAD`. Every one deployed to production individually
and verified green before the next started; no revert was needed.

## Headline

**This repository did not need the demolition the brief anticipated, and saying
so is the main finding.** Phase 0 measured zero dead backend modules, zero
top-level import cycles, zero unintentional cross-file duplication, no secret
ever committed, and both linters clean. It already sat on the conventional
Django 5 / DRF layout. The work that remained was one substantial legibility
change, one piece of dead scaffolding, and a documentation problem.

| | Before | After |
|---|---|---|
| Tracked files | 301 | 294 |
| Net lines | — | **-446** (672 added, 1,118 removed) |
| Function-local intra-project imports | 338 | **196** |
| Re-export barrels | 1 (49 names) | 0 |
| Dependencies | 38 | 38 (nothing added, nothing removable found) |

## The architectural change

Nothing moved. The directory tree is unchanged, because it was already right.
What changed is **where a module's dependencies are written down**:

```
before                                  after
──────                                  ─────
marketdata/tasks.py                     marketdata/tasks.py
  14 header imports                       28 header imports  <- the real graph
  77 imports buried in function            31 deferred, each with a named
     bodies across 1,665 lines                reason (cycle / weight / shadow
                                              / runtime rebinding)
portfolio/views/__init__.py             portfolio/views/__init__.py
  123 lines re-exporting 49 names         docstring only; import from the
  + a 161-line test policing it           concern module, one path per view
```

The module-level import graph was acyclic before and still is — but it used to
be acyclic partly because the real edges were hidden inside function bodies.
Now 142 of them are declared.

## Commits

| # | Commit | What |
|---|---|---|
| 1 | `19ec451` | Phase 0 recon + batch plan |
| 2 | `bf9dd64` | Delete `work/` — 9 files, 363 lines of finished-audit scratch, zero inbound refs |
| 3 | `15f5b25` | `docs/README.md` index by status; delete the superseded quota report |
| 4 | `71a47ba` | Correct three dead paths in `requirements.txt`; drop an f-string and a dead local |
| 5 | `764526e` | Unexport six module-internal frontend bindings |
| 6 | `5541906` | **Collapse the `portfolio/views` re-export barrel** |
| 7 | `a090cf2` | Hoist imports: `marketdata/tasks.py` (46 of 77) |
| 8–11 | `2262745`…`547a44c` | Hoist: `services/valuation`, `views/analytics`, `services/ledger`, `portfolio/tasks` |
| 12–19 | `803b99a`…`8d69f0e` | Hoist: `services/returns`, `views/valuation`, `archive`, `live/fetcher`, `calendars`, `admin_api`, `services/optimization`, `ingest` |
| 20 | `e0b27cb` | `ARCHITECTURE.md` |

Verification on every backend batch: `ruff check .`, the full suite (**1153
passed, 1 skipped** — identical before and after), `import config.asgi`, and the
Celery registry still listing **30 tasks**. On every push: the CI pipeline, then
`/api/health/`, `/api/health/ready/`, `/api/health/prices/` and the app root on
production, plus a 401 (not 404) on authenticated routes to prove they still
resolve.

For the barrel removal specifically, the evidence was stronger than a test run:
the fully resolved URL map — **257 routes**, each with its name and its view
class's dotted path — is byte-identical before and after.

## What the hoist taught, and why 196 imports stayed put

The tool refused to move an import for four reasons, each of which it checked
rather than assumed. Three of the four were discovered by something breaking:

1. **Runtime rebinding (≈60 imports).** A function-local import resolves the
   attribute on the module object at *call* time; a module-level import binds
   once at import time. So `patch("marketdata.fetchers.fetch_derivatives")`
   reaches the deferred form and sails past the hoisted one. **Nine tests failed
   on this** before the rule existed — and it is a property of any runtime
   rebinding, not only of tests. Aliased imports count too: the patch targets the
   *source* name, so `import market_state as current_market_state` is still
   reachable, which cost two more failures to learn.
2. **Transitive scientific-stack weight (≈20).** `portfolio/services/
   optimization.py` imports cvxpy, which drags in scs and its bundled OpenBLAS.
   The tool hoisted it into `portfolio/tasks.py` — directly against that file's
   own comment — which would have loaded that library in the live price worker,
   on a vCPU where that build SIGILLs. Caught by reading the diff, not by a test.
   A flat "is this a third-party package" check cannot see it: the module is
   first-party and the weight is two hops away.
3. **Deliberate shadowing (≈15).** `marketdata/tasks.py` defines a Celery task
   `run_archive_state` and, inside its body, imports archive's plain function of
   the same name. Hoisting would have silently rebound the call to the task
   wrapper. Caught by ruff F811.
4. **Genuine cycle guards (6) and conditional imports inside `try`/`if`.**

## Bugs and drift found, not fixed

- **`CODAL_EXTRACTION_ENABLED` is read by nothing.** It is set in the local dev
  `.env` but the flag the code reads is `CODAL_ENABLED`
  (`config/settings.py`). Anyone who set it believing they were toggling Codal
  ingestion changed nothing. Only in a gitignored file, so there is no repo
  change to make — but worth knowing.
- **`requirements.txt` cited three files that do not exist** (`pricing/
  optimization.py`, `pricing/returns.py`, `marketdata/codal_extract.py`). Fixed
  in commit 4; comment-only, but it was the first place someone would look.
- **The dev `.env.example` is missing.** `README.md`'s quick-start says "create
  `.env` with your BRS + TSETMC keys", and only `.env.production.example`
  survives, so a new contributor has nothing to copy. A hook on this machine
  blocks `.env*` writes, so this needs a human to add it — content proposed below.

## Not done, and why

**The compose consolidation (B18) was abandoned mid-batch, with evidence.**
The two files share 58% of their non-comment lines, and you approved
consolidating them into base + override after I flagged the risk. Building it
showed the plan is not merely risky but wrong:

`docker-compose.yml` publishes Postgres `5432`, Redis `6379`, the API `8000` and
Vite `5173` to the host. `docker-compose.prod.yml` publishes **nothing** — it
joins `vps-edge` and is reached over the Docker network. Compose merges `ports`
by **concatenation**, and an override cannot remove an inherited binding. I
verified this directly rather than trusting the docs:

```
$ docker compose -f base.yml -f override.yml config     # override omits ports
services:
  db:
    ports:
      - published: "5432"        # still there
```

So dev-as-base would publish the database on a VPS that hosts three other
stacks. The alternative — a neutral base plus two overrides — means three files
where there are two, and changes both `deploy.sh` and every dev instruction.
Neither is simplification. My stated gate was "if the rendered config is not
byte-identical, stop and report"; it cannot be, for exactly this reason. The
duplication stays and `ARCHITECTURE.md` rule 4 records why.

**Left alone deliberately:** `Ops.jsx` (2,389 lines) and `Dashboard.jsx` (1,716)
— large but correct, self-contained, and carrying the most e2e coverage in the
app; splitting them is taste-driven churn. The `Dashboard`/`Family` near-
duplicate performance table — they differ on purpose (`Family` tone-colours the
percentage). `tests/legacy_oracle/` — duplicates `live/extractor.py` by design as
the frozen parity oracle. Auth, throttling, CORS, CSP, nginx, the CI workflow,
and every migration.

## Suspected dead — needs runtime confirmation

None of these were deleted; none has enough evidence.

- `marketdata/burst_probes.py` — reachable only through a beat entry and a
  deferred import from `config.celery`. Alive by configuration. *Instrumentation:*
  it already writes `WorkflowRun` rows; query for `claim_burst_probes` outcomes
  over 30 days before anyone calls it dormant.
- `portfolio/management/commands/clean_mispriced_data.py` (last touched
  2026-07-24) and `marketdata/management/commands/clean_invalid_candles.py`
  (2026-08-08) — one-shot repair commands. Management commands are reachable only
  by string; deleting them is a human decision, not a static one.
- The 196 remaining deferred imports include roughly 40 that are plainly
  redundant re-imports of a name the module already binds identically at module
  scope (`from datetime import timedelta` inside a function of a module whose
  header already has it). Deleting those is provably a no-op, but it is a
  *deletion* batch, not a *restructuring* one, and mixing the two is what makes a
  commit unrevertible. Left for a follow-up.

## Recommended follow-ups, ranked

1. **Configure SMTP in production.** Still the standing P0 in
   `docs/PRODUCTION-READINESS.md`: `EMAIL_HOST` is unset, so Django falls back to
   `localhost:25` and every password reset silently fails while returning 200.
   Deployment config, not code.
2. **Add `portfolio-saas/.env.example`** (blocked by a local hook). Minimum
   contents: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=1`, `ALLOWED_HOSTS`,
   `CORS_ORIGINS`, `POSTGRES_DB/USER/PASSWORD`, `BRS_API_KEY`, `TSETMC_API_KEY`,
   `VITE_API_URL=`, `CODAL_ENABLED=0`, `SENTRY_DSN=`. Every other one of the ~130
   variables `settings.py` reads has a working default.
3. **Delete the ~40 redundant re-imports** as its own batch (above).
4. **Act on `PRODUCT-EVALUATION.md`'s backlog item #13** — "delete the dead
   endpoints rather than wiring them". That predates this pass and names specific
   endpoints; it needs a product decision, not a refactor.
5. **Re-check the BRS subscription size.** ~280 req/day of real demand against a
   1,500/day plan, per the quota report deleted in commit 3. Either widen what it
   feeds or drop the tier.
6. **Frontend has no type checker.** eslint's `no-undef` catches the missing
   import; nothing catches a wrong prop shape. Not a refactor — a project.

# Portfolio-SaaS Implementation Plan

**Point-in-Time Correctness → Retrospective Optimization → Market Discovery**

Status: proposed · Created 2026-08-01 · Owner: parham

This document is written to be executed by AI agents or engineers without further
clarification. Every decision is already made. Where a default was assumed, it is
recorded in section 10.

---

## 1. Summary

The infrastructure is stronger than a feature-gap reading would suggest. Verified
live against the running Docker stack:

- Warehouse holds **3,339,211 candles across 1,084 symbols back to Jalali 1380
  (2001)**, plus 3,131,958 daily stock rows and 123,120 gold/FX rows (from 1348).
- `portfolio/services/optimization.py` already implements four genuine
  construction methods — Max Sharpe, Min Volatility, Risk Parity (convex ERC in
  cvxpy), and HRP — with Ledoit-Wolf shrinkage, per-asset and per-class caps, and
  an efficient frontier.
- `portfolio/services/diagnostics.py` already covers annualized volatility,
  Sharpe, Sortino, Calmar, max drawdown, historical VaR/CVaR, and diversification
  ratio.
- Ingestion is quota-governed (9,800 req/day), market-state aware, and split
  across dedicated Celery queues (`archive` vs live pricing).

**The portfolio math is not the gap. The gap is that every analytic is hard-wired
to "now" and to the 8 assets currently held.**

Four verified blockers stand between the current code and the stated goal:

1. **Backdating is impossible.** `Transaction.timestamp` uses
   `auto_now_add=True` (`backend/portfolio/models.py:198`). All 4 existing trades
   stamp the same day. The "exact transaction dates if they exist" requirement is
   structurally unreachable.
2. **Historical net worth is silently wrong.**
   `compute_dynamic_net_worth_series` imports `Transaction` and its docstring
   promises "adjusting holding quantities backward using trade ledger events" —
   then never does it (`backend/portfolio/services/valuation.py:225`). Every past
   date is valued with *today's* quantities, so any past buy inflates the entire
   history.
3. **No point-in-time layer exists.** Zero occurrences of `as_of`, backtest, or
   walk-forward in the codebase. `daily_returns_matrix()` always ends at today
   with `DEFAULT_HISTORY_DAYS = 180`. "The best portfolio in 1401" is
   uncomputable.
4. **No benchmark.** `marketdata_marketindexdata` is **empty (0 rows)**, so alpha,
   beta, tracking error, and "did I beat the market" cannot be computed at all.

The central architectural move is **one parameterization, not a second engine**:
thread `as_of` and `universe` through
`daily_returns_matrix → diagnostics → optimize`. The 5-year retrospective, the
yearly time machine, and market discovery then all become thin callers of the
solver that already works. This is what makes the plan a phased extension rather
than a rewrite.

### Locked decisions

| Decision | Choice |
|---|---|
| Universe scope | Full market discovery (all eligible instruments, not just held) |
| Return basis | Dual: nominal Toman + USD-real, both always computed |
| Sequencing | Correctness first, then backtest engine |
| Backtest realism | Annual rebalance + TSE-realistic costs + liquidity filter |
| Monetization | Retrospective engine is the Pro hook |

---

## 2. Measured Baseline

Record these facts; do not re-measure. All values captured 2026-08-01 from the
live stack.

| Fact | Value | Consequence |
|---|---|---|
| Candles / symbols / range | 3,339,211 · 1,084 · 1380→1405 Jalali | 5-year backtest is feasible today |
| Daily stock history | 3,131,958 rows · 1,064 symbols | Redundant with candles; candles are the source of truth |
| Gold/FX history | 123,120 rows · 38 symbols · from 1348 | Deep hard-asset history |
| Crypto history | 3,232 rows · 2,688 symbols | Sparse; mostly unusable per-symbol |
| Candle timeframes | `1d_adj` (1,056 syms) · `1d_unadj` (1,059 syms) | Adjusted series exists for returns |
| Active `Asset` rows | 8 (~6 usable after house/variance filters) | Optimizer searches a near-empty universe |
| Eligible `MarketInstrument` | 1,194 (1,155 TSE + 39 gold/FX) | The discovery gap |
| `MarketIndexData` | **0 rows** | No benchmark of any kind |
| `ArchiveFetchState` incomplete | 3,787 of 8,126 | Integrity gate must read this, not assume |
| `RejectedRecord` | 3,011 groups / 3,501 occurrences | Parked and invisible in the product |
| 5-year panel query | **4.4s, 676,602 rows discarded** | Bitmap heap scan; needs composite index |
| Risk-free rate | Hardcoded `0.30` in two files | Two sources of truth for Sharpe |
| `Snapshot` payload | `total_value_tomans` only | Attribution must come from the ledger |
| Users / accounts / holdings / txns | 2 / 1 / 4 / 4 | Effectively greenfield user data; migrations are cheap now |
| Provider quota | 9,800/day limit, ~2,767 used | Headroom for index + gap backfill |
| Zero-volume `1d_adj` candles | 15 | Negligible; filter, don't clean |

### Structural observations

- **Jalali strings sort correctly.** `date_time` and `date` are zero-padded
  `YYYY-MM-DD` Jalali text, so lexicographic `<=` slicing is valid for
  point-in-time cutoffs. No schema migration is required for correctness.
- **Snapshots are scalar-only.** Per-asset contribution can never be
  reconstructed from `Snapshot`; attribution must be recomputed from the ledger.
- **Dependency direction is clean.** `portfolio` may import `marketdata`;
  `marketdata` never imports `portfolio`. Preserve this in all new code.
- **User data is tiny.** Schema changes carry near-zero migration risk today. This
  window closes as soon as real users arrive — do the model changes now.

---

## 3. Agent Tracks

| Track | Scope | Depends on | Parallel with |
|---|---|---|---|
| **A** | Phase 0 — ledger, valuation, return metrics | — | B |
| **B** | Phase 0 — indexes, integrity gate, benchmark | — | A |
| **C** | Phases 1–3 — point-in-time core, backtest, discovery | A + B | — |
| **D** | Phase 4 — API + frontend | C's serializer contract | C (stub-first) |

**Rules for parallel agents**

- A and B touch disjoint apps (`portfolio/` vs `marketdata/`) and start
  immediately.
- C is the critical path and must not begin before both A and B are green.
- D builds against frozen stub payloads (section 8) so it never idles waiting
  for C.
- Any agent that needs to change a file outside its track must stop and hand off
  rather than edit across boundaries.

---

## 4. Phase 0 — Correctness Gate

**Blocking. Ship no new features until every item here is green.** Metrics
computed on an unrewound ledger or unvalidated warehouse data are confidently
wrong, which is strictly worse than absent.

### Track A — Ledger & valuation

#### A1. Backdatable ledger
Files: `backend/portfolio/models.py`, `serializers.py`, `services/trades.py`,
`views.py`

- Change `Transaction.timestamp` from `auto_now_add=True` to
  `default=timezone.now`, writable via serializer.
- Add `created_at = models.DateTimeField(auto_now_add=True)` as a separate audit
  column so *event time* (`timestamp`) and *entry time* (`created_at`) stay
  distinguishable. Migration is additive; existing rows keep their values.
- Add `source = models.CharField(max_length=16, choices=("manual", "imported",
  "inferred"), default="manual")` so reconstructed history is never silently
  mixed with recorded history. Backfill the 4 existing rows as `manual`.
- Accept `timestamp` in the trade serializer; expose a date input on the frontend
  trade form defaulting to today.

#### A2. Backdate validation
Validate on write, rejecting with a clear field error:

- Reject future dates (`timestamp > now`).
- Reject dates before the asset's earliest warehouse close — a trade cannot
  precede the price history that would value it.
- Require `quantity > 0`; `side` carries direction.
- When `price_tomans` is omitted on a backdated trade, resolve the warehouse close
  for that Jalali date. **Never fall back to the live `Price` map** — that would
  value a 1399 purchase at today's price. Reuse `marketdata/jalali.py` for
  conversion; do not add a second date helper.
- Allow an explicit user-supplied `price_tomans` to override the resolved close
  (the user may have paid off-market).

#### A3. Undo semantics
`services/trades.py::undo_trade` currently deletes the newest transaction ordered
by `("-timestamp", "-pk")`. Backdating breaks that assumption: insertion order no
longer equals chronological order. Change to delete by explicit transaction id,
scoped to the requesting user's accounts.

#### A4. Ledger-aware historical valuation
New file: `backend/portfolio/services/timeline.py`

- Implement `holdings_as_of(user, account, date) -> dict[str, Decimal]`: start
  from current `Holding.quantity` and walk `Transaction` backward, so
  `qty(t) = qty_now − Σ(buys after t) + Σ(sells after t)`.
- Rewrite `compute_dynamic_net_worth_series` in `services/valuation.py` to call
  it per date, replacing the current use of today's quantities.
- **Fix in the shared function, not per caller.** `/api/snapshots/`, the net-worth
  chart, and the account valuation view all route through it; patching one caller
  leaves the others wrong. This is also the exact primitive Phase 1 consumes.
- Return `null` for dates before a portfolio's first trade rather than
  extrapolating a flat line backward — a portfolio that did not exist has no
  value, and a flat line is a fabricated claim.

#### A5. True return metrics
Add to `timeline.py`:

- Weighted-average cost basis per asset.
- Realized and unrealized P&L.
- **TWR** (time-weighted): neutralizes cash flows, measures selection skill.
- **XIRR** (money-weighted): reflects lived outcome including timing of deposits.
- Both return measures are required because they answer different questions.
  Snapshot deltas alone conflate deposits with market moves and can show a
  "gain" that was purely a transfer in.
- Per-asset attribution is computed from ledger + warehouse prices. Do **not**
  widen `Snapshot` to store per-asset values; it is written once per user per
  fetch and would grow with the asset count.

#### A6. Reconciliation
Add `reconcile_ledger` management command asserting
`Σ ledger == Holding.quantity` per account, exiting non-zero on drift. Holdings
are derived state and nothing in the codebase currently proves they match the
events that produced them.

### Track B — Warehouse integrity & performance

#### B1. Composite indexes
Justification is measured, not speculative. `EXPLAIN ANALYZE` on a single 5-year
`1d_adj` panel query:

```
Bitmap Heap Scan on marketdata_marketcandle
  (actual time=274.026..4359.005 rows=674761 loops=1)
  Filter: ((close_price > 0) AND (timeframe = '1d_adj'))
  Rows Removed by Filter: 676602
Execution Time: 4378.666 ms
```

The only usable index is on `date_time` alone, so `timeframe` is applied as a
post-filter that discards 676,602 rows. A 5-cutoff × 4-scenario study issues this
query shape hundreds of times; unindexed, the retrospective feature is unusable.

- Add index `MarketCandle(timeframe, symbol, date_time)`.
- Add index `GoldCurrencyHistory(symbol, date)`.
- Do **not** add a Gregorian date column yet. Jalali strings are zero-padded and
  sort correctly, so cutoffs work as-is. Revisit only if post-index profiling
  shows Python-side `jdatetime` conversion dominating.

#### B2. Integrity gate (not a report)
New file: `backend/marketdata/integrity.py` + `data_integrity` management command
+ nightly Celery task on the `archive` queue.

`backend/data_quality_report.py` is currently a loose script with a hardcoded
`/app` path and print statements — unusable as a gate. Promote it into a real
module that persists a `SymbolIntegrity` row per symbol:

| Field | Meaning |
|---|---|
| `symbol`, `source` | Natural key |
| `coverage_ratio` | Stored rows ÷ expected trading days in window |
| `max_gap_days` | Longest consecutive missing stretch |
| `rejected_count` | Matching `RejectedRecord` occurrences |
| `passes_gate` | Boolean verdict |
| `reason` | Human-readable failure cause, surfaced in API payloads |
| `computed_at` | Freshness |

Default thresholds (all settings-tunable):

- `coverage_ratio >= 0.95` within the requested window
- `max_gap_days <= 10` trading days
- zero unresolved critical rejections

#### B3. Gate enforcement
Symbols failing the gate are excluded from optimization, discovery, and
backtesting **automatically**, with `reason` returned in the API payload so the
exclusion is explainable rather than mysterious.

This matters concretely: **3,787 of 8,126 `ArchiveFetchState` rows are
`verified_complete=False`** and **3,011 `RejectedRecord` groups (3,501
occurrences)** are parked and invisible. A symbol with a 40-day hole would
otherwise contribute a garbage covariance row and silently distort every
recommendation. Data that cannot be trusted must never enter a recommendation.

Also surface rejected records in the admin portal, ranked by occurrences.

#### B4. Benchmark backfill
`marketdata_marketindexdata` is **empty**, so there is no benchmark and no
alpha/beta/tracking-error is computable.

- Backfill TEDPIX daily history using the existing
  `marketdata/fetchers/index.py`.
- Register `MARKET_INDEX_DAILY` in the `ArchiveFetchState` state machine — it is
  a declared `Endpoint` choice that has never successfully filled, so it never
  self-heals.
- Quota headroom exists (9,800/day limit, ~2,767 used), so this needs no quota
  change.
- Once populated, add beta, alpha, information ratio, and tracking error vs
  TEDPIX to `diagnostics.py`.
- Until populated, **omit** benchmark-relative metrics rather than emit zeros —
  a fake beta is worse than a missing one.

#### B5. Single risk-free rate
`RISK_FREE_RATE_ANNUAL = 0.30` is duplicated in `diagnostics.py:23` and
`optimization.py:37`. Two sources of truth for the number that defines Sharpe is
a latent inconsistency. Move to `settings.RISK_FREE_RATE_ANNUAL`,
env-overridable, imported by both.

---

## 5. Phase 1 — Point-in-Time Core (keystone)

Track C. This phase adds no user-visible feature; it is the foundation every
later phase stands on. Done correctly, Phases 2 and 3 are thin.

### 1.1 Parameterize, do not fork
Add `as_of: date | None = None`, `universe: list[str] | None = None`, and
`basis: str = "nominal"` to:

- `returns.py::daily_returns_matrix()`
- `returns.py::_load_price_panel()`
- `returns.py::_warehouse_series()`
- `returns.py::_load_live_price_panel()`
- `returns.py::correlation_matrix()`
- `diagnostics.py::portfolio_diagnostics()`
- `optimization.py::optimize()`
- `optimization.py::_efficient_frontier()`

`as_of=None` must preserve current behavior **byte-for-byte**. This is what keeps
the existing 30 test modules green and makes the change reviewable.

### 1.2 Truncate inside SQL
Filter `date_time <= as_of_jalali` (and `date <= …` for gold/FX) **inside the
queryset**, never after loading into pandas. Jalali dates are zero-padded
`YYYY-MM-DD`, so lexicographic comparison is correct.

A single unfiltered query is a look-ahead leak that silently invalidates every
downstream result while still returning plausible numbers. This is enforced by
test (section 9), not by review.

### 1.3 Cache-key safety — the most dangerous detail in the plan
`RETURNS_CACHE_KEY` is currently versioned only by a `max(id)` fingerprint across
`Price`, `DailyStockHistory`, `GoldCurrencyHistory`, and `MarketCandle`. Under
that scheme a historical run and a live run collide.

- Include `as_of` and a stable hash of `universe` and `basis` in both the returns
  cache key and the optimizer cache key.
- Extend `invalidate_returns_cache()` accordingly, or switch it to a
  version-prefix bump so it cannot silently miss variant keys.
- A historical run stored under a today-shaped key poisons live results for every
  user, and the failure is invisible because both payloads are well-formed.

### 1.4 Set-based loader
Replace the per-asset Python loop in `_load_price_panel` — which issues one query
per asset — with a single windowed query pivoted in pandas. At 200 symbols × 5
cutoffs the current shape issues thousands of round trips.

Also raise `DEFAULT_HISTORY_DAYS = 180` to a caller-supplied multi-year window;
keep 180 as the live-dashboard default so existing behavior is unchanged.

### 1.5 Symbol-keyed resolution
The pipeline currently iterates `Asset.objects.filter(is_active=True)` and reads
`tse_symbol` / `brs_symbol`. This structurally caps the universe at 8.

Add a resolver keyed on warehouse symbols so instruments with **no `Asset` row**
can be priced and analyzed. `Asset` remains the *holdings* catalog; the
optimizable universe becomes independent of it. This is the single change that
unlocks Phase 3.

### 1.6 Survivorship guard
A symbol must have been trading **at** `as_of` and have coverage across the entire
requested window — not merely exist today.

- Derive investability from candle coverage: no candles after date X means the
  instrument was not investable after X.
- There is no explicit delisting feed from the provider, so coverage is the honest
  proxy.
- Selecting today's symbol list and running it backward is the classic
  survivorship error and would inflate every historical result.

### 1.7 Dual return basis
New file: `backend/portfolio/services/deflator.py`

- Expose `to_basis(series, basis)` supporting `nominal` and `usd_real`, deflating
  by the `usd_cash` daily series already ingested.
- Thread `basis` through returns, diagnostics, and optimizer inputs.
- The API returns **both** bases in one payload so the client toggles without a
  second request.
- Rationale: in a 30–50% inflation regime, nominal Toman returns make gold and FX
  mechanically dominate every ranking. Without deflation, "best performing asset"
  measures inflation exposure rather than skill, and the optimizer would
  systematically over-allocate to hard assets.
- Note the existing `USD_QUOTED_KEYS` conversion in `returns.py` multiplies USD
  prices *up* to Tomans. Deflation is the inverse direction and must not
  double-apply — convert to Tomans first, then deflate the whole panel uniformly.

---

## 6. Phase 2 — Retrospective Engine ("Time Machine")

Track C. This is the feature that answers the original question.

### 2.1 Orchestration only
New file: `backend/portfolio/services/backtest.py`

It calls Phase 1 primitives and contains **no solver logic**. If a cvxpy problem
or a covariance estimator appears in this file, the design has gone wrong and the
work should be reverted to reuse `optimize()`.

### 2.2 Walk-forward loop
For each of 5 annual cutoffs (default: Farvardin 1 of each of the last 5 Jalali
years):

1. Resolve the eligible universe **as of** that cutoff (integrity + liquidity +
   survivorship).
2. Fit expected returns and covariance on the trailing window visible at that
   cutoff (default 3 years, minimum 1 year).
3. Solve all four scenarios (`max_sharpe`, `min_volatility`, `risk_parity`,
   `hrp`) via the existing `optimize()`.
4. Hold the resulting weights forward and evaluate on the **following** year's
   realized returns.

Fit and evaluation windows must never overlap. Overlap is the most common way a
backtest reports returns it could not have earned.

### 2.3 Costs and liquidity
Apply on rebalance turnover, as settings constants so they are tunable without a
code change:

| Constant | Default | Note |
|---|---|---|
| `TSE_BUY_COST` | 0.00372 | Brokerage + exchange fees |
| `TSE_SELL_COST` | 0.0088 | Fees + capital gains/sales tax |
| `GOLD_FX_SPREAD` | 0.01 | Round-trip dealer spread, applied per side |
| `MIN_MEDIAN_DAILY_VOLUME` | tunable | Liquidity floor |
| `MIN_MEDIAN_DAILY_TURNOVER_TOMANS` | tunable | Guards against cheap illiquid names |

- Costs apply to the *change* in weights, not the whole portfolio.
- `MarketCandle.volume` is already populated, so the liquidity filter needs no new
  ingestion.
- Frictionless weights would make thinly traded small-caps look like winners; that
  is the single most misleading output a retrospective engine can produce.

### 2.4 Persistence analysis
Report, across all cutoffs:

- Per-asset **persistence score**: in how many of the 5 annual optimal portfolios
  the asset appears, and its average weight when present.
- Per-class stability: how Gold / Stock / Cash / Crypto allocations drifted year
  over year.
- Themes that held versus one-year anomalies.

This directly answers "what themes have stayed the same" and is the most
defensible insight in the product: it separates durable structure from
single-year luck, which a single optimization run cannot distinguish.

### 2.5 Counterfactual
Compare the user's real ledger-derived history (Phase 0's `holdings_as_of`)
against each scenario's simulated history over the same window. This is the
literal "what would the best choice have been" answer, and it is only honest
because Phase 0 fixed the quantity rewind.

### 2.6 Persistence & async execution
New models:

```
BacktestRun
  user (FK, nullable for global studies)
  params_hash        # unique with basis + universe_hash + integrity_version
  basis              # nominal | usd_real
  universe_hash
  integrity_version  # invalidates when data quality changes
  status             # queued | running | ready | failed
  progress           # 0-100
  error
  created_at, completed_at

BacktestYear
  run (FK)
  cutoff_date        # Jalali
  scenario
  target_weights     # JSON
  realized_metrics   # JSON: return, vol, sharpe, max_dd, cost_drag
  benchmark_delta    # vs TEDPIX, null until B4 lands
  excluded_symbols   # JSON with reasons
```

- Execute as a Celery task on the existing `archive` queue, already isolated from
  live pricing in `config/celery.py`. Never inside a request cycle.
- Report `progress` so the client can show real feedback on a multi-minute job.
- Identical `params_hash` returns the stored run instead of recomputing.
- `integrity_version` in the key means improving data quality correctly
  invalidates stale studies.

### 2.7 Extended metrics
Once TEDPIX is populated (B4): alpha, beta, information ratio, tracking error,
rolling Sharpe, and per-class leaders ranked on risk-adjusted terms rather than
raw return.

---

## 7. Phase 3 — Market Discovery (8 → 1,194 instruments)

Track C. This is where the tool stops being a personal tracker and becomes a
product that can tell someone what they *should* consider.

### 3.1 Candidate universe
New file: `backend/marketdata/universe.py`

Resolve candidates as:

```
MarketInstrument.eligible = True
  ∩ SymbolIntegrity.passes_gate = True
  ∩ liquidity filter (median volume + turnover, as-of)
  ∩ survivorship (trading at as_of, covered across window)
```

This decouples "what can be optimized" from "what is owned" — the prerequisite
for recommending assets the user does not yet hold.

### 3.2 Pre-filter before covariance
A 1,194 × 1,194 Ledoit-Wolf covariance estimate on ~252 daily observations is
severely ill-conditioned (far more parameters than observations) and slow.
Shrinkage reduces but does not rescue this.

- Rank candidates by liquidity and history completeness.
- Cap the working universe at a tunable `MAX_UNIVERSE_SIZE = 200`.
- Record which filter dropped each symbol, and return that list in the payload.

Explainable exclusions are a product feature, not debug output: a
recommendation engine that silently drops 994 instruments is not trustworthy.

### 3.3 Watchlist
New models `Watchlist` / `WatchlistItem`, scoped per account.

`optimize()` and the backtest accept `universe_mode`:

| Mode | Meaning |
|---|---|
| `held` | Current behavior — only assets in the portfolio |
| `watchlist` | Held + user-curated candidates |
| `market` | Full eligible catalog (**default**) |

Support per-symbol force-include and force-exclude overrides.

### 3.4 Auto-provision assets
When a user acts on a recommendation for an instrument with no `Asset` row,
create it on demand from `MarketInstrument` metadata (name, symbol, class,
currency).

Important: `Asset.save()` calls `full_clean()`, and `Asset.clean()` validates the
symbol against `MarketInstrument` with `eligible=True`. Provisioning must flow
through that validation path rather than bypass it with `bulk_create` or
`update_fields`.

### 3.5 Class leaders
Report best performers per asset class on **risk-adjusted** metrics (Sharpe,
Sortino, Calmar), never raw return, and in both bases. Raw-return leaderboards in
an inflationary currency are a ranking of volatility exposure.

---

## 8. Phase 4 — API Surface & Frontend

Track D. Build against these contracts before C lands; they are frozen here.

### 4.1 Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/backtest/` | JWT + Pro | Create run → `{run_id, status}` |
| GET | `/api/backtest/<id>/` | JWT + Pro | Poll status / fetch result |
| GET | `/api/backtest/` | JWT + Pro | List past runs |
| GET | `/api/discovery/` | JWT + Pro | Ranked candidates + exclusion reasons |
| GET/POST | `/api/watchlist/` | JWT | Manage candidate list |
| GET | `/api/performance/` | JWT | TWR, XIRR, cost basis, realized/unrealized |
| GET | `/api/integrity/` | JWT + staff | Per-symbol data quality |

Extend existing endpoints with `?basis=nominal|usd_real` and `?as_of=` where
meaningful. Default `basis` returns both series in one payload.

### 4.2 Response conventions

- Every historical figure carries an explicit `as_of` stamp.
- Every optimization/backtest payload carries `excluded_assets` with reasons.
- Every payload carries `integrity_version` and `data_complete: bool`.
- Never render a number without its provenance. Trust is the product.

### 4.3 Frontend

New `TimeMachine.jsx`:

- Year selector across the 5 cutoffs.
- Weights-over-time ribbon showing how the optimal portfolio shifted.
- Persistence table (which assets recur, average weight).
- Counterfactual overlay: your actual net worth vs each scenario.
- Async job UX: submit → progress → result, with cached runs returning instantly.

New `Discovery.jsx`:

- Per-class leaderboards on risk-adjusted metrics.
- Visible exclusion reasons for filtered symbols.
- One-click add to watchlist.

Global changes:

- Basis toggle (Toman nominal / USD real) wired through `PortfolioContext.jsx`,
  alongside the existing Toman/USD display toggle — these are different concepts
  and must not be conflated in the UI.
- Backdate field on the trade form.
- TWR / XIRR surfaced on the dashboard next to raw net worth.
- Integrity and rejected-record panels in `AdminPortal.jsx`.

### 4.4 Tiering

| Tier | Capabilities |
|---|---|
| **Free** | Live tracking, accounts, holdings, net worth, history, prices |
| **Pro** | Optimization, Time Machine, discovery, attribution, TWR/XIRR |

- Enforce with the existing `IsPro` permission.
- Add a per-user daily quota on backtest submissions, reusing the
  `ApiRequestQuota` pattern. The backtest is the only endpoint whose compute cost
  scales with user count — the README's "fetch cost is O(sources), not O(users)"
  argument does **not** cover it, and an ungated queue is a trivial denial-of-
  service against your own worker.
- Gate at the queue boundary, not just the view, so a free user cannot enqueue
  expensive work through any path.

---

## 9. Test Plan

Test-type rationale, per the testing protocol: **unit** tests for pure logic
(rewind arithmetic, cost math, deflation) because they are fast, isolated, and
sit at the base of the pyramid; **integration** tests for anything touching
Postgres, since `DISTINCT ON` and Jalali ordering cannot be exercised on SQLite
and the look-ahead invariant only exists at the query layer; a thin **e2e** layer
for the two new user journeys.

All tests require local Postgres (see README — sqlite cannot run this suite).

### 9.1 Unit

- `holdings_as_of` against a hand-built ledger: buys only, buy+sell, partial
  sells, multiple same-day trades, and dates before the first trade.
- TWR and XIRR against known-answer fixtures, including a case where a large
  deposit would fool a naive snapshot-delta calculation.
- Cost model: known turnover → expected drag, asserting costs apply to the weight
  *delta* not the whole portfolio.
- `to_basis` deflation on a fixed FX series, including the USD-quoted-asset path
  to prove no double conversion.
- Liquidity filter boundary conditions (exactly at threshold, zero volume).
- Persistence scoring on a synthetic 5-year weight set.

### 9.2 Integration — look-ahead guard (highest value in the suite)

This is the one test that protects the entire product claim.

- Seed a symbol with a deliberate post-cutoff price spike. Assert
  `daily_returns_matrix(as_of=D)` output is **byte-identical** whether or not the
  post-`D` rows exist in the database.
- Assert no observation with `date > as_of` appears in any returned panel.
- Assert cache keys differ across `as_of` values, and that a historical run never
  populates or reads a live-run key.
- Assert `as_of=None` output is unchanged from the pre-change baseline.

### 9.3 Integration — correctness

- Backdated trade accepted with a historical date; price resolved from the
  warehouse close, not the live map.
- Future-dated and pre-history trades rejected with field errors.
- `undo_trade` deletes a backdated (non-latest) transaction correctly.
- Historical net worth reflects rewound quantities: a portfolio that bought
  mid-window shows a lower value before the buy.
- `reconcile_ledger` catches injected drift.
- Integrity gate excludes an incomplete symbol from optimization output, with the
  reason present in the payload.
- Survivorship guard excludes a symbol with no candles after the cutoff.
- Walk-forward run: fit and evaluation windows provably disjoint.
- Identical `params_hash` returns the cached run without recomputation.

### 9.4 Performance

- Assert the 5-year multi-symbol panel query completes under 500ms after the
  composite index (measured baseline: 4,378ms).
- Assert a 200-symbol, 5-cutoff study completes within an agreed worker budget;
  record the actual figure in this file when first measured.

### 9.5 Regression

- All 30 existing test modules stay green.
- `test_returns_source_selection.py`, `test_optimization.py`, and
  `test_extractor_parity.py` are the highest-risk existing suites — run them first
  after any Phase 1 change.

### 9.6 E2E (Playwright suite already exists)

- Pro user submits a 5-year study, sees progress, then per-year weights and the
  persistence table.
- Free user is gated from Time Machine and discovery.
- Basis toggle changes displayed metrics.

---

## 10. Assumptions & Defaults

Recorded because they were chosen without explicit instruction. Change them here,
in one place, if wrong.

1. **Jalali storage retained.** Dates stay as zero-padded Jalali strings;
   lexicographic comparison is used for cutoffs. No Gregorian column until
   profiling justifies it.
2. **Rebalance cadence:** annual, Farvardin 1, for the 5-year study. Monthly and
   quarterly are deferred until requested.
3. **Trailing fit window:** 3 years default, 1 year minimum. Shorter windows are
   too noisy for covariance estimation.
4. **Benchmark:** TEDPIX, once backfilled. Benchmark-relative metrics are omitted
   until then rather than faked.
5. **Real estate** (`is_house=True`) stays excluded from optimization and
   backtesting — it has no daily price series. It remains in net worth.
6. **Crypto** is largely unusable per-symbol (3,232 rows across 2,688 symbols);
   the integrity gate will exclude most of it automatically. This is correct
   behavior, not a bug to fix.
7. **`MAX_UNIVERSE_SIZE = 200`**, chosen so covariance estimation stays
   conditioned on ~252 observations.
8. **Costs** default to TSE-realistic published figures (0.372% buy / 0.88% sell).
   Verify against your broker's actual schedule and adjust the settings constant.
9. **USD-real is the honesty check, not the default display.** Nominal remains the
   primary displayed basis since it matches broker statements; USD-real is always
   computed and one toggle away.
10. **`DailyStockHistory` is not the returns source.** `MarketCandle` with
    `timeframe="1d_adj"` is, because adjusted closes are required for correct
    return series across splits and dividends. The existing code already does
    this; do not "simplify" it back.
11. **Snapshot schema unchanged.** Attribution comes from the ledger.
12. **No new dependencies.** Everything needed (pandas, numpy, cvxpy, pypfopt,
    scikit-learn, jdatetime) is already installed.

---

## 11. Execution Order

```
Phase 0  ──┬── Track A (ledger, valuation, TWR/XIRR, reconcile)
           └── Track B (indexes, integrity gate, TEDPIX, risk-free)
                        │
                        ▼  both green
Phase 1  ───── Track C (as_of + universe + basis parameterization)
                        │
                        ▼
Phase 2  ───── Track C (walk-forward, costs, persistence, BacktestRun)
                        │
                        ▼
Phase 3  ───── Track C (universe expansion, watchlist, provisioning)
                        │
                        ▼
Phase 4  ───── Track D (endpoints, TimeMachine, Discovery, tiering)
```

Track D starts early against the frozen contracts in section 8.

### Definition of done per phase

- **Phase 0:** a backdated trade produces a correct historical curve;
  `reconcile_ledger` is clean; the integrity gate excludes a known-bad symbol;
  TEDPIX has rows; the panel query is under 500ms.
- **Phase 1:** the look-ahead guard test passes; `as_of=None` is byte-identical to
  baseline; all 30 existing modules green.
- **Phase 2:** a 5-year study returns per-year weights, realized metrics net of
  costs, a persistence table, and the counterfactual — reproducibly, from cache on
  second call.
- **Phase 3:** optimization recommends at least one instrument not currently held,
  with every exclusion explained.
- **Phase 4:** the two journeys work end-to-end under Pro gating, with provenance
  visible on every figure.

---

## 12. What This Plan Deliberately Does Not Do

- **No CPI deflator.** Iranian CPI is contested and needs a new ingestion source.
  USD-real covers the inflation-honesty requirement at a fraction of the cost.
- **No intraday backtesting.** `StockTransactionTick` data exists but daily
  closes are the right granularity for annual rebalancing.
- **No broker integration.** There is no Plaid equivalent for the Iranian market;
  named accounts with a manual ledger remain the honest v1.
- **No full execution simulation.** Slippage modelling and price-limit
  (دامنه نوسان) simulation are deferred; annual rebalancing with realistic costs
  and a liquidity floor is sufficient for the questions being asked.
- **No `Snapshot` widening.** Per-asset attribution is recomputed, not stored.
- **No Gregorian date migration.** Deferred until profiling justifies it.

Each of these is a deliberate scope cut with a stated trigger for revisiting.

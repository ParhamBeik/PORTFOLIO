# Product evaluation — can the app answer the customer's questions?

Static audit, 2026-09-02, against `main` @ `HEAD`.

This scores the product the way a paying customer scores it: not "is the code good" but
"can I get the answer I came for". Seven questions define the job to be done. Q1–Q6 are
things the product already claims. **Q7 is a proposed Pro feature**, scored on the same
rubric so it sits in one table, but measured on *readiness to build*, not on delivery.

Each question is scored twice, because the two fail independently:

- **Backend** — does the data, schema and business logic exist, and are they correct?
- **Frontend** — is what is on screen actually delivering that answer?

**Delivered** is what the customer experiences. It is capped by the frontend: an endpoint
with no client answers nothing.

| Band | Meaning |
|---|---|
| 9–10 | Answers it completely, states its own limits, no known way to mislead |
| 7–8 | Answers it; a named edge case or caveat is unhandled |
| 5–6 | Partially; the user must infer or go elsewhere for part of it |
| 3–4 | Machinery exists but the answer is not reachable, or not framed as the answer |
| 0–2 | Cannot be answered here |

---

## Scorecard

| # | Question | Backend | Frontend | **Delivered** | One-line verdict |
|---|---|:---:|:---:|:---:|---|
| Q1 | Live, congruent portfolio tracking | 9 | 8 | **8** | Price truth and liabilities are visible; secondary analytics still need freshness stamps |
| Q2 | Add/edit/delete trades; trustworthy back-dated P/L | 8 | 7 | **7** | Ledger, CSV import, dated events, and explicit opening-date behavior are reachable |
| Q3 | Historical data breadth, freshness, comparison | 8 → **9** | 7 | **7** | Single-asset history and four comparison modes are reachable; crypto depth is stated |
| Q4 | Diversification as an allocation change | 9 | 7 | **7** | The best-conceived module in the codebase, split across two pages and never told when to act |
| Q5 | Optimal portfolio by scenario and risk tolerance | 9 | 6 → **9** | 6 → **9** | Risk tolerance, a position cap and the resampling bands all now have controls; only the Pro tier is unbuilt |
| Q6 | Operator monitoring and diagnosis | 9 | 9 | **9** | Excellent — but it watches the warehouse, not the users |
| Q7 | ML limit-to-limit signal *(proposed)* | 2 | 0 | **0** | Substrate is real and rare; nothing is built, and the order book it needs is unreachable |

### Re-scored after the direct-source migration (2026-09-01)

Only one number moved, and it is worth being precise about why so little did.

**Q3 backend 8 → 9.** The investable universe went from **38 analysable
instruments to 53**, and crypto from **2 to 17**. ETH, XRP, ADA, DOGE, LTC, BNB,
SOL, TRX, DOT, AVAX, LINK, SHIB, UNI, ATOM and FIL previously had *no history at
all*, which meant they could not enter the returns matrix and were therefore
invisible to the optimizer, the comparison view and every risk figure —
regardless of whether anyone held them. BTC deepened 1,037 → 2,491 sessions and
USDT/Toman 1,035 → 2,756, back to 2018-11-27.

**Q1 backend stays 9**, but is materially more robust: the gold/FX lane no
longer competes for the BrsApi wallet that archive could drain before dawn, and
two independent exchanges now cross-check every crypto quote each cycle.

**Nothing on the frontend column moved, and that is the honest finding.** This
work was entirely backend and data. Q2 (4), Q3 (4) and Q5 (6) are limited by
missing UI — a CSV import, a single-asset price screen, a risk-tolerance
control — and none of that was touched. More history behind a screen that does
not exist is still invisible to the user. Q3's *delivered* score is unchanged at
4 for exactly that reason: the backend can now answer the question, and the
product still cannot ask it.

**Q7 remains 2/0.** It is blocked on the TSE order book, which is blocked on
network reachability, not on modelling. `scripts/setup_iran_egress.sh` is the
unblock; until it runs, nothing about Q7 can move.

### Second pass, same day: the frontend column finally moves

The first pass concluded that nothing reached the user. Two things now do.

**Q3 delivered 4 → 6.** The comparison chart draws the TSE index. It had been
hard-coded unavailable for a sound reason — BrsApi sells the index as a live
snapshot and no history at any price, so there was nothing honest to plot. TGJU's
`bourse` series supplied **2,751 daily closes back to 1393-02-08**, and the
endpoint now returns `tse_index` in both `series` and `labels` with an empty
`unavailable` list. Verified against production for the family account: 64
points, indexed 101.2 → 171.8.

**Q4/Q5 backend 9 → 9, but with analytics that previously did not exist.**
`HISTORICAL_BENCHMARK_ENABLED` was never defined in settings, so it defaulted
False and **beta, alpha and tracking error had never once been computed in
production** — only under `override_settings` in tests. They now run: the family
portfolio measures beta 0.243, alpha 0.963, tracking error 0.292 against TEDPIX.

**Q5's investable universe also grew** from 38 analysable instruments to 53, and
crypto from 2 series to 17.

The remaining product limits are now narrower: secondary analytics still need
freshness stamps, data-quality has no dedicated client, and the Pro tier is not
modelled. **Q7 (0)** remains blocked on network reachability.

**Weighted read.** Q1, Q4, Q5 and Q6 are at or near a shippable paid standard. Q2 and Q3
are where the product loses a customer, and both fail the same way: the backend already
does the work and no screen exposes it. **21 of 53 endpoints have no frontend caller** —
that single fact explains most of the gap between the backend and delivered columns.

### Third pass, same day: Q5's three missing controls are built

The second pass named four unmet pieces in Q5. Three are now closed, and one of
them turned out to be a genuine backend gap rather than missing UI.

**Risk tolerance is now a number the user states.** `EfficientFrontier.efficient_risk`
was the one entry point nothing in the repo called; it is now the `efficient_risk`
scenario — maximize expected return subject to an annualized volatility ceiling.
The page offers Cautious/Balanced/Growth/Aggressive (10/20/35/50%), and picking
one selects the scenario it produces rather than leaving the tab on an answer
computed for someone else's appetite. A ceiling **below the minimum-variance
floor** is unreachable by any weights, so it degrades to that floor, marks
`target_volatility_below_minimum`, and prints the shortfall in warn colour —
"the calmest book your assets can make is 18%" is the useful reply to "I want
2%"; an error is not.

**"A limited number of assets" now exists.** This was scored as missing UI and
was not: no cardinality constraint existed anywhere in `optimize()`. It is a
mixed-integer constraint, so the implementation is the standard greedy
relaxation — solve, keep the N largest positions, re-solve on **just those
columns**. The re-solve is the point: truncating and renormalizing leaves
weights that are optimal for a universe the reader is no longer holding. The
payload labels it a heuristic and the page repeats the label. Where the weight
caps make N positions arithmetically unable to reach full investment (three
assets under a 30% cap reach 0.90), it degrades and says so rather than raising
at a user who expressed a preference, not a violation.

**`/api/optimization/robustness/` has a screen.** 200 bootstrap re-solves,
gated behind a button with its own longer timeout, because that cost is exactly
why it was split from `my-optimal` in the first place. The table sorts by band
**width**, not weight — the widest bands are the rows worth reading — and prints
how many draws converged so a thin sample cannot pass for a confident band.

**Found while testing the cap: `_enforce_caps` was deleting weight.** Each
asset's share of the redistributed slack is clipped at its own headroom, so one
pass routinely places less than the whole slack — and the tilt toward
already-held assets makes that the normal case, since it hands the slack to
assets near their cap while the room sits on the empty ones. The residue was
silently dropped, because the next round recomputes slack from scratch. Four
assets under a 32% cap can reach 1.0; with two of them zeroed by the solver it
placed **0.64**, and the caller raised *"constraints are infeasible at full
investment"* on a feasible problem. It now places the slack in passes until the
headroom is gone. This affected every scenario, not just the new ones.

**Q5 frontend 6 → 9, delivered 6 → 9.** Not 10: "with the Pro plans" is a
product-model gap — tiers were removed in accounts migration `0005` and every
endpoint is open to any authenticated user. That is a decision to make, not a
defect to fix.

Backend test count 939 → 958.

---

## Q1 — "Can I track my portfolio live, and is it congruent everywhere?"

**Backend 9 · Frontend 7 · Delivered 7**

### What exists

Prices are global, not per-user: one `DISTINCT ON (asset)` read plus a Redis cache
(`portfolio/services/valuation.py:125`), so fetch cost is O(sources), not O(users).

Price resolution is deliberately **one path with a stated precedence**, and this is the
strongest congruency work in the repository:

- `_archive_replacements` (`valuation.py:375`) decides live-vs-warehouse by *trading
  session*, not by wall clock, and refuses a close that is behind a price already held —
  the failure where the market shuts and today's real close is replaced by yesterday's.
- Forward-fill is bounded at `MAX_FORWARD_FILL_SESSIONS = 5` (`valuation.py:50`), counted
  with `marketdata.calendars.sessions_between`, and the same bound is enforced in all three
  price paths (`guard_price_map`, `value_as_of`, `compute_dynamic_net_worth_series`).
- The server **declares** what the client must not guess: `quality_status`,
  `unit_price_currency`, `priced_at`, `age_seconds`, `quantity_step`, `price_unit_status`
  (`valuation.py:791–828`). A TSE quote is Rial and everything else is Toman; that boundary
  is declared per row rather than inferred from magnitude.
- `_live_quality_status` (`valuation.py:618`) separates *live* / *stale* / *quota* /
  *fallback* / *manual* / *unavailable*, and refuses to launder a missed session into a
  green badge during a market closure.

The frontend treats congruency as a first-class concern too. `applied_basis` is echoed on
both `/valuation/` and `/snapshots/` and every page reads `data.basis || basis`
(`Dashboard.jsx:144`, `:380`, `:842`) so a currency switch cannot draw Toman under a dollar
axis while the next request is in flight. `CoverageNote` (`Dashboard.jsx:1334`) explains why
the risk percentages and the Holdings Weight column have different denominators — two
numbers that genuinely disagree on one screen, reconciled in prose. `MeasurementNote`
(`:1304`) prints the observations actually used against the window requested, plus the
±1/√n error bar. `PerformanceUnavailable` (`:103`) explains why this card says "locked"
while My Optimal shows a return.

That habit — *state the limitation on screen, next to the number* — is the product's real
differentiator and is defended throughout this report.

### What is missing

1. **Only two surfaces poll.** `pollMs: 60000` appears on Dashboard (`:1544`) and Family
   (`:303`). My Optimal is additionally cached 300s server-side (`views.py:1613`) and Best
   Overall is a nightly snapshot, so their "Δ value" columns are priced from a different
   vintage than the Dashboard hero the user just left. Best Overall prints
   `Computed {as_of}` (`BestOverall.jsx:111`); **My Optimal prints no as-of at all**, so its
   staleness is invisible.
2. **Two independent answers to "what should I trade".** My Optimal renders
   server-computed `rebalance_trades`; Best Overall differences the weights **in the
   browser** (`BestOverall.jsx:194`). Two code paths for one question, on different price
   vintages — they can disagree, and nothing reconciles them.

### Score rationale

Backend 9: the price-truth engineering is better than the bar. Frontend 7: excellent
per-row honesty, undone by a total that silently excludes something the user owns money on.

---

## Q2 — "Can I easily edit my transactions, and will my old portfolio's P/L be right?"

**Backend 8 · Frontend 4 · Delivered 4**

### What exists

`LedgerEntry` (`portfolio/models.py:402`) is an event log, not a mutable position table.
Check constraints per kind, `reversal_of` for corrections, `external_id` +
`ImportBatch(file_hash)` for idempotent re-import, and an explicit `rights_issue` kind for
افزایش سرمایه whose average-cost mathematics is derived in place (`models.py:440`,
`performance.py:114`) — deliberately not an opening (which would void every prior cost
basis) and not a zero-price buy (which trips the "no price recorded" sentinel).

Back-dated pricing goes through `resolve_historical_price` (`ledger.py:1143`): warehouse
close for the date, then today's live tick when the bell has rung but the archive has not
landed, then the distilled daily bar. `assert_not_before_history` (`:1109`) refuses a date
earlier than the first price that exists rather than inventing one.

`_position_metrics` (`performance.py:69`) is honest about what it does not know: an opening
position or a price-less buy sets `cost_basis_known: false` and every derived money field
becomes `null` rather than a plausible wrong number. Cost basis and P&L are deliberately
**not** behind the 90-day annualization bar (`performance.py:196`) — those are correct from
the first buy, and withholding them left the panel blank for three months.

`ledger_complete` / `tracking_started_at` are **derived, never user-set**: the first
`opening_cash` or `opening_position` sets both (`ledger.py:169–174`), and
`rebuild_projections` recomputes them from the ledger (`:655`, `:693`). So there is no
hidden switch the user must find to unlock TWR/XIRR — recording what you own is enough.

`AddTransactionDialog.jsx` is the best-executed component in the app: five steps in the
order a person knows the answers, no domain vocabulary on screen (`ACTIONS` at `:54` is the
only place plain language maps onto a ledger kind), a Jalali date field, per-asset
Rial-vs-Toman labelling (`:212`), refusal rather than silent rounding of a fractional share
(`:285`), and a plain-sentence summary before saving.

### What is missing

1. **"I already own it" is intentionally baseline-dated.** The wizard now explains the
   effective timestamp returned by the server and steers multi-year histories toward dated
   purchases, which preserve the acquisition date.
2. **No way to see how far back an asset can be dated.** `assert_not_before_history` raises
   with the earliest available date; that reaches the user as a red error rather than as a
   bound the date picker already knew.
3. **`/accounts/<id>/data-quality/` and `/api/integrity/` have no client** — the two
   endpoints built to answer "can I trust my own numbers".

### Delivered in this pass

CSV import now validates and commits through the existing idempotent endpoints; the wizard
exposes dividend, fee, and rights-issue events; and replayed openings report when the shared
tracking timestamp replaced the selected date.

<!-- Historical notes retained below for auditability. -->
<!--
2. **"I already own it" silently discards the date you picked.** Openings must share one
   timestamp (`ledger.py:169`), so `record_existing_position` clamps `occurred_at` to
   `account.tracking_started_at` (`ledger.py:244`) and `LedgerListCreateView` routes every
   non-house opening through it (`views.py:418–441`). The server is honest — its docstring
   states the acquisition date is lost, and it returns the timestamp actually used. **The
   wizard discards that response** (`AddTransactionDialog.jsx:376`) and its review sentence
   has already promised "as of {when}". For a book accumulated over years, every "already
   owned" position collapses onto one date, silently. The correct path — a series of dated
   BUYs — exists and works, but nothing steers the user to it, and the date field's own
   hint says *"leave it on today for now"*.
3. **Three modelled kinds are unreachable.** The backend models `dividend`, `fee` and
   `rights_issue`; the wizard offers none. On the TSE, dividends and rights issues are the
   two events that move cost basis most, so the basis drifts from reality with no way to
   correct it.
4. **No way to see how far back an asset can be dated.** `assert_not_before_history` raises
   with the earliest available date; that reaches the user as a red error rather than as a
   bound the date picker already knew.
5. **`/accounts/<id>/data-quality/` and `/api/integrity/` have no client** — the two
   endpoints built to answer "can I trust my own numbers".
-->

### Score rationale

Backend 8: correct and unusually careful; the single-timestamp opening rule is a real
modelling limitation for the stated use case, not a bug. Frontend 4: you can enter a
portfolio, but you cannot enter a *multi-year* one accurately or in bulk, and the one
lossy path is the one the UI makes easiest.

---

## Q3 — "Is historical data available and updated, and is comparison meaningful?"

**Backend 8 · Frontend 4 · Delivered 4**

### What exists — and the depth is better than expected

The verification read settles how far back "many years" actually reaches, and it is
asymmetric by asset class:

| Asset class | Source | Depth | Cost |
|---|---|---|---|
| TSE stocks (unadjusted OHLC) | `Tsetmc/History.php` type 0 | **entire series, ~4,616 rows ≈ 17 yrs** | **1 request/symbol** |
| TSE stocks (adjusted) | `Candlestick.php` type 3 | entire series, ~4,256 rows | 1 request/symbol |
| TSE real/legal (حقیقی/حقوقی) | `History.php` type 1 | entire series | 1 request/symbol |
| Gold / currency | `Gold_Currency_Pro.php?history=2` | **back to 1390 (~15 yrs)** | 1 request/symbol |
| Crypto, commodity, index, options | none — `_RETIRED_ARCHIVE_ENDPOINTS` (`archive.py:169`) | **since this deployment started**, via `MarketDailyBar` distilled from live polling | n/a |

All four historical endpoints are `Nature.HISTORICAL_FULL` — no date paging, no window
policy, one request buys the whole series (`endpoints.py:168–208`). So stock and gold depth
is bounded only by whether that symbol's single fetch has landed, not by any ceiling. That
is a genuinely strong position. **Crypto is the honest exception** and should be said out
loud in the product: there is no provider history at all, so a crypto position cannot be
back-dated before the day this system started watching.

Around it: per-plan quota accounting where `Endpoint.plan` is the one declaration of which
API key bills which call (`endpoints.py:44`), gap-driven backfill (`archive.py`), market and
per-symbol closure calendars (`calendars.py`), an integrity gate that distinguishes
*our warehouse is incomplete* from *the data is gappy* (`integrity.py`), and a returns cache
keyed on a fingerprint of every table that can change the panel
(`returns._price_version_fingerprint`).

`services/comparison.py` answers "was this worth it" four ways — counterfactual, two
holdings, portfolio vs benchmark, lump sum — and deliberately routes through
`returns._load_price_panel` rather than opening a fifth price-resolution path, because "the
unit boundary is exactly where this project's bugs live". The page states its own shortfalls
(`Comparison.jsx:172`, `:179`) when the window asked for exceeds the history available.

### What is missing

1. **Freshness is signalled on one page only.** The per-row Status column, the ≥50% stale
   banner (`Dashboard.jsx:1131`) and the pricing glossary (`:689`) are Dashboard-only.
   Comparison, My Optimal and Best Overall carry no freshness indicator, so a user reading a
   comparison has no way to know whether it includes today.
2. **Nothing surfaces the crypto asymmetry.** A user comparing a crypto holding over "1Y"
   gets whatever exists, with no statement that the provider has no history for it.

### Score rationale

Backend 8: deep, well-governed, honest about gaps internally. Frontend 4: the primary ask
has no screen at all.

---

## Q4 — "Tell me exactly how to change my allocation to be properly diversified"

**Backend 9 · Frontend 7 · Delivered 7**

### What exists

`portfolio/services/diversification.py` is the best-conceived module in the codebase, and
its docstring frames the problem exactly as the question does — diversification is the one
risk reduction that costs no expected return, so measure it directly rather than inferring
it from a Sharpe ratio. It needs no return forecast, which is the point:

- `risk_contributions` — RC_i = w_i·(Σw)_i / (w'Σw), summing to 1.0 by Euler's theorem, so
  each entry reads directly as "this holding is X% of my risk".
- `effective_bets` (inverse Herfindahl over *risk*) beside `effective_holdings` (over
  *weights*). Ten perfectly correlated gold coins score 10 on one and ~1 on the other, and
  the gap between them is the finding.
- `diversification_ratio`, and `concentration_gap` sorted worst-offender first.
- `diversifier_candidates` — "what should I buy next?" ranked by volatility **removed**, never
  by past return, with the O(n) two-asset derivation justified in place rather than building
  a singular 296×296 covariance.

The optimizer backs this up with correlation-cluster caps and a combined hard-asset sleeve
cap (`optimization.DEFAULT_CONSTRAINTS`), so it cannot quietly pile into USD + gold + coins
that all move together.

The Dashboard Risk card stacks four panels rather than hiding three behind tabs
(`Dashboard.jsx:1401` — the docstring says tabs meant three of four were never seen), with
captions written for a non-quant. My Optimal's diversification table adds a "more spread /
more concentrated" change column and an unusually honest disclosure that the optimizer
concentrates when concentrating pays, and that this is a trade being made rather than a
mistake (`MyOptimal.jsx:369`).

### What is missing

1. **Measurement and action live on different pages, in different vocabularies.** The
   diversification numbers are on the Dashboard; the trade list that would fix them is on My
   Optimal, behind a scenario tab framed as an optimizer choice. Risk Parity and HRP are
   precisely "the diversified allocation", and neither is labelled as such.
2. **Nothing says when to act.** No rebalancing bands, no drift threshold, no alert. The
   user is shown a gap and left to decide whether it is large enough to matter.

### Score rationale

Backend 9: correct, well-chosen, and honest about what it is not. Frontend 7: everything is
present and well explained, but the journey from "I am not diversified" to "here is the
trade" crosses two pages and a vocabulary change.

---

## Q5 — "Show me the best portfolio for different scenarios and risk tolerances"

**Backend 9 · Frontend 9 · Delivered 9** *(was 9 · 6 · 6; see "Third pass" above)*

### What exists

The strongest engineering in the repository, and notably more honest than most commercial
tools:

- Six scenarios (`optimization.SCENARIOS`), with `FORECAST_FREE_SCENARIOS` marking the four
  that need no return forecast — and the UI defaults to `min_volatility` for exactly that
  reason (`MyOptimal.jsx:42`).
- Ledoit-Wolf shrunk covariance throughout; per-asset, per-class, per-correlation-cluster
  and per-sleeve caps; a post-hoc clip-and-renormalise pass for the two methods with no
  native cap primitive.
- `MIN_OBSERVATIONS_PER_ASSET = 10` with the failure it prevents recorded in place: a
  200×200 covariance from 45 rows produced a live Sharpe of 16.
- Credibility ceilings that are **surfaced, never suppressed** (`SHARPE_CREDIBILITY_CEILING`,
  `EXPECTED_RETURN_CREDIBILITY_CEILING`), an explicit
  `expected_return_provenance.mean_standard_error` error bar rendered on screen
  (`MyOptimal.jsx:418`), a `forecast_free` badge, `limitations`, `degraded`,
  `excluded_assets` with reason codes, `proxy_groups`, `constraints_floored`, and a frozen
  sleeve that *holds* short-history assets at current weight rather than selling them.
- An efficient frontier plus a 400-draw Dirichlet cloud over the user's **own** assets, so
  the chart describes a portfolio they can actually build (`views.py:1551`).

### What was missing, and what closed it

1. ~~**No risk-tolerance control.**~~ **Closed.** The `efficient_risk` scenario maximizes
   expected return subject to an annualized volatility ceiling the user picks. It is the
   only scenario that cannot solve unasked, so the endpoint runs it only when a ceiling
   arrives and the nightly Best Overall precompute (its own `SCENARIOS` tuple in
   `portfolio/tasks.py`) is untouched. An unreachable ceiling degrades to the
   minimum-variance floor and says so, rather than erroring or quietly showing a number
   that is not the one that was asked for.
2. ~~**No cardinality constraint.**~~ **Closed.** `max_assets` on the constraints dict.
   Greedy relaxation of a mixed-integer constraint — solve, keep the N largest, re-solve
   on that subset — labelled as a heuristic in the payload and on screen. Clamped to a
   3-asset floor, since two things are not a portfolio and `UniverseTooSmall` fires below
   it in two other places.
3. **Constraints are still fixed from the user's side** — *partially*. The two the
   question names (`max_assets`, `target_volatility`) are now controls. The weight caps
   (`max_weight_per_asset`, per-class, per-cluster, sleeves) remain policy: `OptimizationView.post`
   accepts them in a body no client sends, and **no page calls `/api/optimization/` at
   all**. Deliberate for now — a user who raises their own gold cap to 100% has not
   diversified, they have turned off the thing this page exists to do — but it is a
   product decision, not an implementation gap.
4. ~~**`/api/optimization/robustness/` has no client.**~~ **Closed.** "How much of this is
   signal?" on the My Optimal page: 200 bootstrap re-solves behind an explicit button,
   sorted by band width, with the converged-draw count printed.
5. **"With the Pro plans" does not exist.** Tiers were removed (accounts migration
   `0005_remove_user_pro_expires_at_remove_user_tier`); `views.py:5` states every endpoint is
   open to any authenticated user. This is a product-model gap, not a defect — but if Pro is
   the plan, none of the gating is built.

### Score rationale

Backend 9: the mathematics, the guards and the honesty are all at a professional standard.
Frontend 9: all three of the question's nouns — *scenarios*, *risk tolerances*, *limited
number of assets* — now have a control, and the anti-overfitting answer is on screen.
Neither reaches 10 while the Pro tier the question names is unbuilt and the weight caps
remain non-negotiable from the UI.

---

## Q6 — "Can an admin see what is stored and diagnose what is wrong?"

**Backend 9 · Frontend 9 · Delivered 9**

### What exists

The best-served question by a distance. `Ops.jsx` (1,968 lines) over `admin_telemetry.py`
(1,020) plus `coverage_report`, `evidence`, `integrity`, `provenance` and the `WorkflowRun`
ledger:

- Live-price coverage split held-vs-catalog; warehouse refresh backlog ordered by
  **days since last success** rather than a boolean; archive job lifecycle; per-endpoint
  stacked counts; per-plan quota wallets with "two separate wallets — spending one never
  frees the other" written into the panel copy; worker ping; queue depth; disk projection;
  error-code ranking; a 15-minute ingest view; a Codal panel that goes quiet when the
  subsystem is off rather than reporting a frozen backlog.
- **Statistical honesty in the telemetry itself**, which is rare: row counts come from
  `pg_class.reltuples` so the request path never `COUNT(*)`s 58M rows, they are clamped to a
  running maximum only for tables nothing deletes from (`APPEND_ONLY_COUNT_KEYS`), and an
  hour the collector skipped stays `None` all the way to the chart instead of being plotted
  as a spike to zero.
- `coverage_report.classify_archive_state` is the one place the "does it owe rows / has it
  ever landed a payload" decision lives, and `Ops.jsx:archiveJobVariant` mirrors it — the fix
  for a classifier that once reported 52.8% of the warehouse as damaged when every one of
  those jobs had `missing_rows == 0`.
- Per-asset evidence with named claims and a `suggested_cli`, reachable from the Dashboard
  itself for staff (`Dashboard.jsx:1513`).
- Actions (retry / refresh / recompute integrity) require `confirm`, refuse when the
  provider breaker is open or the broker is unreachable, and write **both** a
  `SystemLogEvent` and a `WorkflowOutcome` (`admin_api.py:215–232`) — an audit trail, not a
  fire-and-forget button.

The "Needs attention" panel is a direct answer to the question's *"without knowing every
single detail, understand what is wrong"*.

### What is missing

**The console watches the warehouse, not the users.** Nothing surfaces user-domain health:
`ledger.projection_drift` (`ledger.py:722`) exists and has no console; there is no aggregate
view of accounts whose performance calculation is blocked and why, of valuation `excluded`
reasons, or of failed trades. An operator can read "the warehouse is healthy" while every
user's P/L is wrong. Secondary: the Django admin index is monkeypatched to inject telemetry
(`config/urls.py:14`) — a working but surprising second console.

---

## Q7 — ML flagging limit-to-limit intraday reversals *(proposed Pro feature)*

**Backend 2 · Frontend 0 · Delivered 0** — see the [feasibility annex](#q7-feasibility-annex).

**Nothing is built.** No model, no label, no feature store, no training pipeline, no serving
path. The nearest thing is `nightly_asset_signals` → `AssetSignalSnapshot` over
`portfolio/services/signals.py`: RSI, MACD and trend stance per symbol — rule-based, not
learned, daily, not intraday. Two of its properties are the right patterns to reuse:
`passes_integrity` is stored **on the row** so the caveat travels with the number, and
`backtest_crossover` shifts its position by one bar specifically to refuse the lookahead lie.

And `AssetSignalSnapshot` is **written nightly and read by nothing** — no view, no endpoint,
no frontend. A whole scheduled job producing output no one can see.

Backend scores 2 rather than 0 because the *substrate* is real, rare and well-governed —
detailed in the annex. It is not 3+ because none of the three things a model needs (labels,
features, training) exists, and the one input the described strategy actually turns on is
not purchasable from the current provider.

---

## Cross-cutting

### Produced but never surfaced

**21 of 53 endpoints had no frontend caller** when this was first measured; `/optimization/robustness/` has since been wired, so the current count is 20. Reproduce with:

```bash
cd portfolio-saas && python3 - <<'PY'
import re, pathlib
root = pathlib.Path(".")
paths = []
for f, prefix in [("backend/portfolio/urls.py", "/api/"),
                  ("backend/marketdata/admin_api.py", "/api/admin/")]:
    for m in re.finditer(r'path\(\s*"([^"]*)"', (root/f).read_text()):
        paths.append(prefix + m.group(1))
blob = "\n".join(p.read_text() for p in root.glob("frontend/src/**/*.js*"))
def used(p):
    pat = r'\$\{[^}]*\}'.join(re.escape(s) for s in re.split(r'<[^>]+>', p))
    return re.search(pat, blob) is not None
for p in paths:
    if not used(p): print("NO CLIENT:", p)
PY
```

| Endpoint | Blocks | Note |
|---|---|---|
| `/accounts/<id>/imports/preview/` · `/commit/` | **Q2** | CSV import — the single biggest delivery gap |
| `/accounts/<id>/liabilities/` (+detail) | **Q1** | mortgages are netted off the total, invisibly |
| `/prices/history/` | **Q3** | single-asset price chart |
| `/assets/returns/` | Q3 | returns matrix + correlation |
| `/analytics/asset-ranking/` | Q3 | per-holding Sharpe/Sortino ranking |
| ~~`/optimization/robustness/`~~ | ~~Q5~~ | **Wired** — "How much of this is signal?" on My Optimal |
| `/accounts/<id>/data-quality/` · `/integrity/` | **Q2** | "can I trust my own numbers" |
| `/insights/` | Q1/Q4 | rule-based findings |
| `/performance/` · `/accounts/<id>/valuation/` | — | duplicated by scoped variants; likely dead |
| `/transactions/` · `/<id>/` · `/<id>/undo/` | — | superseded by the ledger routes; likely dead |
| `/accounts/<id>/ledger/<id>/reverse/` | Q2 | reversal is modelled, unreachable |
| `/optimization/snapshots/` · `/latest/` | — | Best Overall reads its own endpoint instead |
| `/marketdata/webhook/brsapi/` | — | correctly server-to-server, not a gap |

Plus `AssetSignalSnapshot`, a nightly-written table with no reader at all.

Roughly a third of the backend is invisible. Some of it is genuinely dead and should be
deleted rather than wired up — that decision is per row, and the backlog below only proposes
wiring the ones that unblock a question.

### What to defend

The pervasive habit of **stating the limitation next to the number** is this product's real
differentiator, and it should be treated as a standard, not a nicety. Concrete instances
worth protecting: `CoverageNote`, `MeasurementNote` and the ±1/√n error bar, the estimated-
points count on the trend chart ("47 of 66 points are rebuilt"), the CPI projection notice,
the `forecast_free` badge, `constraints_floored`, `proxy_groups`, the "more concentrated"
delta on the diversification table, `cost_basis_known`, the comparison shortfall notes, and
`passes_integrity` stored on the row. Most portfolio tools present a number with no error
bar; this one refuses to. Any new feature — Q7 above all — must meet that bar.

---

## Fix backlog

Ranked by *question unblocked* × *user reach*, not by effort. The first two are what a
customer hits in their first hour.

| # | Fix | Unblocks | Reach | Effort | Lands in |
|---|---|---|---|---|---|
| ~~1~~ | ~~**CSV import UI** — preview/commit wizard over the existing endpoints~~ | ~~Q2~~ | — | — | **Done 2026-09-02** |
| ~~2~~ | ~~**Show and edit liabilities** — a line under the hero, plus CRUD~~ | ~~Q1~~ | — | — | **Done 2026-09-02** |
| ~~3~~ | ~~**Stop silently discarding the opening date** — read the returned timestamp back and say "recorded at your tracking start"; steer multi-year entry toward dated buys~~ | ~~Q2~~ | — | — | **Done 2026-09-02** |
| ~~4~~ | ~~**Single-asset price history page** over `/prices/history/` + `/assets/returns/`~~ | ~~Q3~~ | — | — | **Done 2026-09-02** |
| ~~5~~ | ~~**Dividend / fee / rights-issue in the wizard**~~ | ~~Q2~~ | — | — | **Done 2026-09-02** |
| ~~6~~ | ~~**Risk-tolerance control** — target volatility via `efficient_risk`, plus a max-assets cap~~ | ~~Q5~~ | — | — | **Done 2026-09-01.** Both controls ship on My Optimal |
| 7 | **As-of stamp + freshness badge off the Dashboard** (My Optimal, Comparison, Best Overall) | Q1, Q3 | everyone | S | the three pages |
| 8 | **One trade-advice path** — have Best Overall consume server-computed trades instead of differencing in the browser | Q1, Q4 | everyone | S | `BestOverall.jsx:194`, `views.py` |
| 9 | **Surface `data-quality`** *(`robustness` done 2026-09-01)* | Q2 | everyone | S | `Dashboard.jsx` |
| 10 | **Frame Risk Parity / HRP as "the diversified allocation"**, and add drift bands | Q4 | everyone | S | `MyOptimal.jsx`, `Dashboard.jsx` |
| 11 | **User-domain panel in Ops** — projection drift, blocked performance, excluded reasons | Q6 | operator | M | `admin_telemetry.py`, `Ops.jsx` |
| 12 | **Q7 v1 spike** — daily-bar + real/legal classifier (see annex) | Q7 | Pro users | L | new `marketdata/ml/` |
| 13 | **Delete the dead endpoints** rather than wiring them | maintenance | — | S | `portfolio/urls.py`, `views.py` |

### Round-2 workstreams (2026-08-31)

Two research tracks, each with its own document. Both are independent of the table above and
can run in parallel with it.

| # | Workstream | Why it ranks here | Document |
|---|---|---|---|
| A | **Probe VPS egress to `cdn.tsetmc.com` / `api.nobitex.ir`** | one command; gates workstream B entirely, exactly as `codal.ir` gates Codal today | [`DATA-SOURCES.md`](DATA-SOURCES.md) |
| B | **Move the TSETMC lane off BrsApi** | turns a ~2-year tick backfill into ~2 months and adds the order book Q7 needs. Gold/FX already use TGJU first, including mapped historical backfills | [`DATA-SOURCES.md`](DATA-SOURCES.md) |
| C | **Nobitex crypto history** | small, keyless, documented; repairs the named Q2/Q3 gap that crypto cannot be back-dated before this deployment started | [`DATA-SOURCES.md`](DATA-SOURCES.md) |
| D | **nginx compression + `<meta description>` + CSP** | hours of work; the compression fix alone removes ~70% of transfer for every real user | [`LIGHTHOUSE.md`](LIGHTHOUSE.md) |
| E | **Split status colour tokens into fill vs text variants** | measured WCAG failures on every P&L figure and on the primary button — 3.15:1 and 3.64:1 | [`LIGHTHOUSE.md`](LIGHTHOUSE.md) |
| F | **Lighthouse CI gating the existing `frontend` job** | `deploy` already depends on that job, so enforcement needs no new wiring | [`LIGHTHOUSE.md`](LIGHTHOUSE.md) |

**On ordering.** Q7 sits at #12 deliberately. Shipping a signal model on top of a product
that cannot import a trade history (#1) and hides a mortgage from the net worth (#2) is the
wrong order — the model would be judged by customers who already distrust the totals it is
computed against.

---

## Q7 feasibility annex

### The substrate is real, and unusual for a retail product

**The label needs no new data.** `DailyStockHistory` (`marketdata/models.py:298`) carries
`py` (previous close — the band's anchor), `pmin`, `pmax`, `pf` (open), `pl` (last),
`pc` (closing/پایانی), `plp`, `pcp`, `tno`, `tvol`, `tval`. "Traded at or near the lower
band and finished at or near the upper band" is a pure query over rows already stored.

**Training data for a daily v1 costs about one afternoon of quota.** `History.php` type 0
(prices) and type 1 (real/legal) are both `HISTORICAL_FULL` at ~4,616 rows per request, so
the entire multi-year panel for ~1,346 symbols is **~2,700 requests** — roughly a quarter of
one day's TSETMC wallet, and much of it is already stored. Contrast that with the tick tape
below.

**The band percentage is not a stored field.** `StockSymbolMetadata` has `market` and
`market_board` but no limit percentage, and Iranian bands differ by board (بورس / فرابورس /
بازار پایه tiers) and have changed over time. **Infer the effective band empirically** per
symbol-era from the clustering of `pmax/py` and `pmin/py`, rather than hardcoding a table
that will be wrong for بازار پایه today and wrong again after the next rule change.

**Features available now, at daily granularity.** The full حقیقی/حقوقی breakdown — buy/sell
counts, volumes and values — on both `DailyStockHistory` and `RealLegalHistory` (1.6M rows).
Per-capita real buy (`buy_i_value / buy_count_i`) is the single most-used feature in exactly
this strategy. Plus `base_volume`, `shares_count`, `free_float`, `market_cap`, `sector`
(`StockSymbolMetadata`), `MarketIndexData` for market regime, and `CorporateAction`. Codal is
dormant (`CODAL_ENABLED=0`; codal.ir is unreachable from the VPS), so news features are
unavailable until that network path exists.

**Intraday path.** `StockTransactionTick` — a TimescaleDB hypertable, ~58M rows — carries
`price`, `volume`, `time`, `row`, `canceled`. Enough to reconstruct the executed tape:
trade-size distribution, aggression proxies, time-to-first-touch of the band, cancellation
bursts, volume clustering at the band price. `reconcile_tick_volume` (`validation.py:440`)
already quarantines days whose ticks do not sum to the candle volume, so label and feature
quality has a gate in place.

### Two hard limits that change the product, not just the accuracy

**1 — There is no order book *from the current provider*.** No bid/ask ladder, no queue
depth for TSE stocks. Nothing in `endpoints.py` or `fetchers.py` returns صف خرید/فروش;
`MarketSnapshot.bid_price`/`ask_price` exist only for crypto, commodity and ETF-NAV. The
described pattern — *"there are orders at that price and a wave of buys eats the sell
queue"* — **is** the order book. Executed ticks reveal the queue was consumed only after it
was consumed, so a model built on today's data learns the aftermath, not the setup.

> **Update (2026-08-31, superseding the paragraph above).** TSETMC serves this directly:
> `cdn.tsetmc.com/api/BestLimits/:InsCode/:DEven` returns the historical bid/ask queue per
> symbol per day, with no API key. See [`DATA-SOURCES.md`](DATA-SOURCES.md). This removes
> the ceiling rather than working around it, and it is the strongest single argument for
> migrating the TSETMC lane off BrsApi. It is gated on one unverified fact — whether the VPS
> can reach `cdn.tsetmc.com` at all, given that `codal.ir` times out from it today. The v1
> staging below is unchanged either way: v1 needs no order book, and it is what tells you
> whether the label carries signal before you spend anything on the rest.

**2 — There is no live intraday tape, so the notifier cannot be served today.**
`Tsetmc/Transaction.php` is `Nature.HISTORICAL_PER_DAY` — one request buys one **completed**
symbol-day (`endpoints.py:225`). The live price loop stores last price only, no intraday
volume. So a model can be trained and can publish a **pre-open** ranking; an in-session
"buy now" alert needs an endpoint that is not currently bought. Train and serve are blocked
independently and must be scoped separately.

### Correcting the "30/90-day ceiling"

Two different constants, and neither caps training data:

- `SYNTHETIC_HISTORY_MAX_DAYS = 90` (`valuation.py:923`) bounds a *fabricated* net-worth
  chart for users with no recorded snapshots. Unrelated to market data.
- `MARKETDATA_TICK_WINDOW_DAYS = 90` (`settings.py:274`) is the **seed** for a new tick
  state, not a cap. `grow_tick_windows` (`archive.py:1125`) widens every completed window by
  90 days per pass, floored at each symbol's `InstrumentListingHistory.first_seen` and
  clamped at `MAX_TICK_WINDOW_DAYS = 12_000`; 31 symbols are already at the clamp. The
  ceiling is already lifted by design.

### What actually limits tick depth: quota

Ticks buy one symbol-day per request. `settings.py:263` puts the backlog at **~5M requests**.
The TSETMC wallet is ~10,000/day (`MARKETDATA_PLAN_LIMIT_TSETMC`) less a 150 safety margin
less the live reserve, of which ticks take `MARKETDATA_TICK_QUOTA_SHARE = 0.70` → roughly
6,800 symbol-days/day → **on the order of two years to complete the tape**.

And today, `INTEGRITY_FAILURE_RATE_THRESHOLD = 0.85` exists precisely because 1,072 of 1,346
symbols fail the integrity gate *purely because the tick backfill is unfinished*
(`settings.py:400`). A tick-fed model trained now would learn from whichever symbols the
claim ordering happened to favour — that is a selection bias, not a sample.

### Recommended staging

**v1 — buildable now. No new data, no new dependency.** Daily-bar + real/legal supervised
classifier; label from `DailyStockHistory`; `scikit-learn==1.5.0` is already pinned (it ships
`LedoitWolf` for the optimizer). Publishes a **calibrated pre-open probability**. Its real
job is to prove the label carries signal *before* two years of quota is committed to ticks.
Evaluate on a walk-forward split against the base rate, and publish the base rate beside the
hit rate.

**v2 — tick-fed.** Intraday path features, gated on a per-symbol coverage bar. Reuse the
`SymbolIntegrity` / `passes_integrity` pattern rather than inventing a second one.

**v3 — in-session notifier.** Blocked on an intraday feed that does not exist. Do not promise
it in Pro copy.

**Against RL.** Reinforcement learning needs a fill simulator, and fills are decided by the
order book — the one input that is missing. A backtest that assumes a fill at the band price
is exactly the lie `signals.backtest_crossover` already shifts a bar to avoid. Calibrated
supervised classification is the honest fit for what this data can support. Torch or an RL
framework would also add native dependencies to a VPS that has already lost a worker to a
bundled-BLAS `SIGILL` — see the `scs==3.2.11` pin comment in `requirements.txt`.

**Framing obligation.** This is a buy/sell signal sold to retail investors. It needs a
standing accuracy-and-limits surface — hit rate, base rate, sample size, and an explicit
statement that the model cannot see the order queue. The codebase's existing habit of
stating limitations on screen sets the bar; this feature must clear it, not lower it.

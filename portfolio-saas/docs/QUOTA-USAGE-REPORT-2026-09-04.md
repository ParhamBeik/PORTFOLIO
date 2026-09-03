# Provider quota usage report — 14 days to 2026-09-04

Source: `marketdata_apirequestquota`, `marketdata_workflowrun`, `marketdata_archivefetchstate`,
`portfolio_price` on the production VPS (`root@89.106.206.4`, `portfolio-saas-db-1`).
All clock times are **Tehran local**; the quota day runs Tehran-midnight to Tehran-midnight.

---

## 1. Headline

The TSETMC wallet is not underspent because of the provider, the network, or a
missing key. It is underspent because **the archive pacing gate and the archive
retry handler disagree about what "wait" means**.

`quota.reserve_request` raises `QuotaExhausted(reason="archive_paced")` to mean
*"you are ahead of your pro-rata share of the day — come back in a few minutes."*
`archive.run_archive_state` catches that exception and defers the state to
**the next quota day**:

```python
except QuotaExhausted as exc:              # archive.py:819
    rollover = next_quota_day_start(now)
    state.last_attempt_at = now
    state.next_attempt_at = rollover       # <-- 24h, for a 5-minute condition
```

`QuotaExhausted.reason` already distinguishes `plan_blocked`, `bucket_exhausted`,
`live_reserved` and `archive_paced`. The handler reads none of them.

Because almost the entire backlog falls due at Tehran midnight, and midnight is
the exact minute when `archive_allowance_now` is at its floor
(`MARKETDATA_ARCHIVE_BATCH_SIZE = 120`), the sequence every night is:

1. ~7,000 states wake at 00:00.
2. The first ~120 requests are permitted. The wallet is now "on pace".
3. Every remaining state is refused `archive_paced` and pushed to the *next*
   midnight — where the same thing happens again.
4. The wallet sits idle for the remaining 22 hours with ~4,000 requests unspent.

**Confirmed live during this investigation.** At 00:01:36 on 2026-09-04, ninety
seconds into the new quota day, `archive_used` was already exactly `120`. Three
minutes in: 156 paced refusals logged and 189 states already stamped
`next_attempt_at >= 2026-09-05 00:00`.

Right now **6,863 of ~7,200 incomplete archive states carry
`last_error = "Daily quota unavailable."`** — they are all parked on a pacing
wait that was misfiled as an exhausted wallet.

A second, smaller amplifier sits at `archive.py:827`: one paced refusal also
bulk-pushes *every other state leased in the same tick* to rollover.

---

## 2. Daily spend, by wallet

`limit` is `0` on every row because BrsApi only discloses its ceiling on **error**
responses; the ceilings below are our configured expectations
(`MARKETDATA_PLAN_LIMIT_TSETMC/_BRS`). The one day the provider ever stated its
own number was 2026-08-25, and it said **9,800**, not 10,000.

| Day (Tehran) | TSETMC used | of 10,000 | archive | live | BRS used | of 1,500 | archive | live |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 08-21 | 9,214 | 92% | 7,664 | 1,549 | — | — | — | — |
| 08-22 | 9,763 | 98% | 8,425 | 1,237 | — | — | — | — |
| 08-23 | 9,800 | 98% | 9,079 | 671 | — | — | — | — |
| 08-24 | 9,601 | 96% | 9,101 | 499 | — | — | — | — |
| 08-25 | 9,800 | 98% | 9,743 | 56 | 464 | 31% | 210 | 254 |
| 08-26 | 10,034 | 100% | 10,034 | **0** | 151 | 10% | 5 | 146 |
| 08-27 | 1,877 | 19% | 1,826 | 0 † | 572 | 38% | 242 | 330 |
| 08-28 | 1,780 | 18% | 1,727 | 0 † | 544 | 36% | 207 | 337 |
| 08-29 | 6,283 | 63% | 6,151 | 131 | 907 | 60% | 510 | 397 |
| 08-30 | 6,451 | 65% | 6,440 | 10 | 624 | 42% | 270 | 354 |
| 08-31 | 4,033 | 40% | 3,905 | 127 | 700 | 47% | 306 | 394 |
| 09-01 | 4,571 | 46% | 4,440 | 130 | 497 | 33% | 219 | 278 |
| 09-02 | 5,035 | 50% | 4,916 | 118 | 147 | 10% | 62 | 85 |
| 09-03 | 5,968 | **60%** | 5,967 | 0 † | 114 | **7.6%** | 31 | 83 |

† `live_used = 0` on 08-27, 08-28 and 09-03 is **correct**, not a fault: those are
Thursdays and Fridays, the Iranian weekend, so there is no TSE session to poll.
The `live_used = 0` on **08-26** is the known incident (archive drained the wallet
by 03:43 and the breaker took live down with it).

Cumulative shortfall over the 8 days since 08-27: roughly **32,000 unspent
TSETMC requests** against a backlog of ~7,200 incomplete states.

---

## 3. Interval report — when requests actually happen

### A. Provider HTTP requests per hour

```
           0 1 2 3 4 5 6 7 8 91011121314151617181920212223     total
  08-21   @ : : : = + . : _ . . _ + + : : : : : - - + : *     8871
  08-22   @ @ : : + = : : : : * : : : % + = * - .   _ : :     9486
  08-23     . .   . .   _ _ _ # @ # + @ : : : . . . . . .     9670
  08-24   @ @ # . . . . : : : : : : : : : : : . : : . : .    10202
  08-25   @ @ @ @ - . . _ _ _ _ : : . - . . . . . . . . .    10327
  08-26   @ @ @ @ .                       . . . . . . . .    10185
  08-27   + + = . . . . . . . . . . : . . # . . . . . . .     2449
  08-28   = . . . : . . : : : : : : : : = - - - - : : - :     2314
  08-29   = : : % : : % + - + + = - - @ : : : : : . . . .     7190
  08-30   + . : * = = # * - - - - - - % - - - - : - - - -     7075
  08-31   + - : # = : : - - - - - - - # : : : : - : : : :     4733
  09-01   + - : = - : * + : - - - - - % : : : : - : : : .     5068
  09-02   + = - + * - = - - - - - : : % : : - : : . : : .     5182
  09-03   + = - = + + + + = = = - - = - = = - - - : . . .     6082
  legend: '_' nothing   . : - = + * # % @   ('@' ~= 1500+ req/hr)
```

Two regimes are visible:

- **Through 08-26** — the day is front-loaded (`@ @ @ @` in hours 00–04) and the
  wallet reaches its ceiling. This is the *old* bug: burn everything before dawn.
- **From 08-27** — the midnight burst is capped, but nothing replaces it. The day
  flattens into a low plateau of roughly 250 req/hour and never reaches the
  ceiling again. The pacing fix stopped the overrun and created the underrun.

Hour-by-hour for 09-03, with pacing refusals broken out:

| Hour | HTTP req | `archive_paced` refusals |
|---|---:|---:|
| 00 | 409 | **6,384** |
| 01 | 252 | **1,822** |
| 02–23 | 89–536 (avg ~250) | **0** |

After 01:00 the pacing gate refuses *nothing* — there is simply nothing left that
the scheduler considers due. `archive_tick` fires every 15s (5,739 runs on 09-03)
and **2,658 of them, 46%, ended in `no_due_states`**. The archive is idle by its
own scheduling, not by quota.

### B. Live price fetch cycles per hour

```
           0 1 2 3 4 5 6 7 8 91011121314151617181920212223     total
  08-21   * # * * # + _ - _ : : _ * # * # * # * # # * # *      188
  08-22   # * * * * + # * + * # * * # * * = - = : - _ = _      170
  08-23   _ _ _ _ _ _ _ _ _ _ = @ @ * # * # # * # * * # *      164
  08-24   # # * # # * # * # @ @ @ @ * # * * * # * # * * #      293
  08-25   # * # # * * * _ _ _ _ * @ # * * * # * * # * # #      216
  08-26   * # # * # * # * % @ @ @ @ # * # # # * # * # * *      308
  08-27   # * # * # * * * * # * * # * * * * # * # * * * *      247
  08-28   # * # * # * * * * * # # * # * # # * * # * # * #      252
  08-29   * * # * * * # * # @ @ @ @ # # # # # # # # # # :      304
  08-30   : _ _ + # # # # # # # # # # # # # # # # # # # :      239
  08-31   _ : _ _ _ _ _ # # @ @ @ @ # # # # # # # # # # :      232
  09-01   _ : _ _ _ _ _ # # @ @ @ @ # # # # # # # # # # :      238
  09-02   _ : _ _ _ _ _ # # % @ @ @ # # # # # # # # # # :      228
  09-03   _ : _ _ _ _ _ # # # # # # # # # # # # # # # # :      197
  legend: '_' nothing   . : - = + * # % @   ('@' ~= 20+ cycles/hr)
```

- Through **08-29** prices were written around the clock.
- From **08-30** hours 00–06 go dark. That is `market_state.live_job_keys`
  working as written: `gold_currency` only runs in `OPEN` or `CLOSED_DAYTIME`,
  and `DAYTIME_START = (7, 0)` / `DAYTIME_END = (23, 0)`. **Overnight is zero
  BRS calls by design.**
- Cadence inside the active window is ~12 cycles/hour — one every 5 minutes —
  against `MARKETDATA_LIVE_INTERVAL_DAYTIME = 240` (4 min, 15/hr). The extra
  minute is task latency.
- Assets receiving live prices fell **12 → 11 → 9** over the fortnight. Worth a
  separate look; it is not a quota question.

Measured on 09-03: 195 distinct fetch cycles, 1,755 price rows, 9 assets,
first write 07:00:39, last 22:58:22.

---

## 4. Why BRS never approaches 1,500

This one is **structural, not a bug**. The BRS wallet has almost nothing to buy:

| Consumer | Ceiling per day | Notes |
|---|---:|---|
| Live `gold_currency` | ~240 | 15/hr × 16 waking hours; 8 overnight hours are zero by design |
| Archive `gold_daily` | ~38 | 38 states, all `verified_complete`, next due 09-05 |
| Archive `crypto_daily` / `commodity_daily` | 1 each | retired from the backfill path (`_RETIRED_ARCHIVE_ENDPOINTS`) |
| **Realistic ceiling** | **~280** | against a 1,500 subscription |

Actual on 09-03 was 114, below even that, because a Thursday spends no
`CLOSED_DAYTIME` hours the way a trading day does.

`bucket_budget(LIVE, BRS)` computes to `min(1200 + 500, 1500 - 150) = 1,350`, so
the live cap never binds either. **The BRS plan is over-provisioned for what the
system currently asks of it** — roughly 5× larger than the workload. That is a
subscription-sizing decision, not something to fix in code.

---

## 5. tgju / nobitex / wallex

These consume **no quota at all** and are not metered anywhere. They are
unauthenticated public endpoints and appear in no counter, so there is nothing to
report on "token usage" for them.

| Origin | Where it is used | Metered? |
|---|---|---|
| `tgju.org` | `archive.py:399` — gold history fallback when BrsApi has no payload | no |
| `api.wallex.ir` | `tasks.py:131` crypto live rows; `backfill_crypto_history` command | no |
| `apiv2.nobitex.ir` | cross-check against wallex in `check_egress` only | no |
| `tsetmc_direct` | present but the origin L3-drops the Frankfurt VPS | unusable |

Note the asymmetry worth acting on: **wallex and nobitex are free and reachable,
while the BRS wallet you pay for is 92% idle.** If crypto live rows are currently
being served from wallex, that is part of why BRS usage keeps falling — the paid
feed is being bypassed by a free one.

---

## 6. An observability gap found on the way

`marketdata/tasks.py:638` records the archive ledger as:

```python
rows_received=state.expected_rows,
rows_accepted=state.stored_rows,
```

These are the state's **cumulative** totals, not this request's delta, and
`rows_created` is never passed at all — so it is `0` on every `archive_state` row
regardless of what was written. Summing them across a day is meaningless:
09-03 reads as "1,385,270 rows received, 0 created", which is an artefact, not a
finding. **The ledger currently cannot answer "how many rows did the archive
actually gain today?"** Every other workflow (`capture_market_snapshots`,
`aggregate_market_daily_bars`) populates `rows_created` correctly.

---

## 7. Recommended fixes, in order of payoff

1. **Make the retry handler read `exc.reason`** (`archive.py:819`). Only
   `plan_blocked` and `bucket_exhausted` deserve a rollover deferral.
   `archive_paced` should defer **minutes**, not a day. This one change unparks
   6,863 states.
2. **Do not cascade a paced refusal** to the rest of the leased batch
   (`archive.py:827`) — restrict that bulk update to genuine exhaustion.
3. **Stagger `next_attempt_at` across the day.** Everything waking at Tehran
   midnight is what collides with the pacing floor. Jitter the rollover deferral
   over the following 24h instead of stacking it on 00:00.
4. **Fix `rows_created` on the archive ledger** so the next report can measure
   yield per request rather than inferring it.
5. **Re-sanity-check the BRS subscription size.** ~280 req/day of real demand
   against a 1,500 plan; either widen what it feeds (overnight gold/crypto
   polling, since those markets do trade overnight) or drop the tier.
6. Separately, look at live-priced assets falling 12 → 9.

---

## 8. On the BrsApi panel

I could not open `https://api.brsapi.ir/Panel/panel.html`. I have no browser
automation available in this session and no access to your browser's
authenticated session, so the panel's numbers are not something I can read for
you — you will need to check the vendor dashboard yourself, which remains the
ground truth for what each key has actually spent.

Worth comparing when you do: our counters are **our own tally of requests we
issued**, not the provider's. The two only reconcile on error responses, via
`quota.reconcile_account`. If the panel shows meaningfully more than 5,968 on the
TSETMC key for 09-03, that difference is spend happening outside these counters.

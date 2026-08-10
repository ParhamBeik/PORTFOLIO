# Warehouse snapshot

**When:** 2026-08-10 ~09:00 UTC
**Source:** Docker Compose Redis/Postgres runtime checks
**Units:** TSE warehouse and portfolio stock quotes = Rial under the legacy one-tenth-share convention; other portfolio values = Toman.

## Current state

| Metric | Value |
|---|---:|
| Archive jobs complete | 7,317 / 8,105 (90.3%) |
| Tick jobs complete | 532 / 1,310 (40.6%) |
| Remaining archive rows | 12,998 |
| Quota used | 2,833 / 9,800 (28.9%) |
| Quarantined records | 22,478 |
| Codal queue | 1,064 and draining |

Candles, unadjusted history, gold, crypto, commodities, and shareholder states are complete. Remaining work is mainly 778 tick states, eight Codal announcement states, and two adjusted-history states.

## Queue correction verified

A fresh 15-minute run after restart kept `live=0` and `archive=0` pending at every sample while Codal fell from 1,094 to 1,064. Moving lightweight producer tasks off their work queues doubled observed Codal drain from about one to about two items per minute; archive completed two additional states without accumulating pending work.

The same window recorded 60 partial and two complete archive-state runs, plus two retries: one provider `ReadTimeout` and one invalid tick payload. All eight long-running services remained healthy.

## Active safeguards

| Issue | Control |
|---|---|
| Scheduler starvation | Producers run on `live`; generated work stays on `archive`/`codal` |
| Queue growth | Pending caps: archive 4, Codal 1; broker inspection fails closed |
| Tick volume noise | 1% relative tolerance before quarantine |
| Hard tick mismatch loop | Rejected dates become known gaps and are not re-requested |
| Missing prerequisites | Tick/adjusted states soft-defer until their source history exists |
| Quota thrash | Exhausted archive work defers to next Tehran midnight |
| Network failures | Existing transient retry/backoff remains active |

Detailed unit policy: [`F1_POLICY.md`](F1_POLICY.md).

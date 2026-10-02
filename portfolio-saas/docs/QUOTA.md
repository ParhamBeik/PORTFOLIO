# BrsApi quota: where every request goes (live reference, verified 2026-10-02)

Read this before reasoning about provider quota. Do not infer billing from an
API key, a path prefix guess, or a single panel glance.

## Two wallets, one key

| Product (panel name) | `plan` in code | Daily limit | Billed paths |
|---|---|---:|---|
| AIO (All In One), expires 1405-09-01 | `aio` (`endpoints.TSETMC`) | 10,000 | `Tsetmc/*`, `Codal/*`, `Market/Gold_Currency_Pro.php` |
| Market_CGCC (free, no expiry) | `market_cgcc` (`endpoints.BRS`) | 1,500 | `Market/Gold_Currency.php`, `Market/Cryptocurrency.php`, `Market/Commodity.php` |

`marketdata/endpoints.py` is the only authority: `_call` bills
`endpoints.billing_product(endpoint, query)`. Market_CGCC **is metered**
(1,500/day); it is not unmetered.

## The counters already match the panel

`reconcile_quota_meters` reads the BrsApi panel itself and stores it in
`ApiRequestQuota.provider_used` (`provider_observation_source="panel"`). The
app's own `used` matched the panel within 5 requests on every day
2026-09-24..10-02. **Read `ApiRequestQuota`, not the browser panel.**

Per-request attribution: `WorkflowRun.quota_attempts`, keyed by `workflow`,
`endpoint` and `source` (provider path). Sum since Tehran midnight
(`quota.quota_day()`) to see who spent what. Totals can differ from the
counter by a few dozen at the day boundary (a run that starts before midnight
bills after it); compare increments between two readings, which match exactly.

## What normal looks like (measured)

| Day type | AIO | Market_CGCC |
|---|---|---|
| Trading day (Sat–Wed) | live ~350–480 (TSE loop); archive takes the rest | live ~1,030–1,090 |
| Thu/Fri, no session | live 0; archive fills to **9,850** (limit − 150 safety margin) | live ~680–810 |

Example, Fri 2026-10-02 by 15:05 Tehran: AIO 9,850 = archive Candlestick 4,664 +
Transaction ticks 2,812 + History 2,178 + Codal 66 + Gold_Currency_Pro 31 +
other 51 (`sync_symbol_metadata`, `catalog_sync`). CGCC = `live_prices` +
`capture_market_snapshots:commodity`.

An AIO wallet at ~9,850 on a closed day is **by design**, not a leak: the live
reserve (`quota.live_reserve_remaining`) shrinks to zero when no session is
left, and archive gets everything else. On a trading day the reserve holds the
live loop's remaining need until Tehran midnight.

## How to check today

```bash
ssh root@45.139.10.12 'cd /opt/apps/portfolio-repo/portfolio-saas && docker compose -f docker-compose.prod.yml --env-file .env.production exec -T backend python manage.py shell -c "from marketdata.models import ApiRequestQuota as Q; from marketdata.quota import quota_day; [print(r.plan, r.used, r.provider_used, r.live_used, r.archive_used, r.other_used) for r in Q.objects.filter(day=quota_day())]"'
```

History of how the plan mapping was established:
[BRSAPI-QUOTA-EVIDENCE-2026-09-22.md](BRSAPI-QUOTA-EVIDENCE-2026-09-22.md).

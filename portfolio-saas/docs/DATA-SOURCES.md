# Replacing BrsApi: measured provider assessment

Probed from the production VPS on 2026-08-31. Companion to
[`PRODUCT-EVALUATION.md`](PRODUCT-EVALUATION.md).

> An earlier draft of this document reasoned from published documentation. Everything below
> has now been measured from the server that would actually make the calls, and **two of its
> conclusions were wrong**. They are corrected in place and called out where they mattered.

## Status: shipped

Gold, FX and crypto are **off BrsApi**. `marketdata/sources/` holds TGJU, Wallex and
Nobitex clients; `portfolio/live/fetcher.py` fetches them alongside BrsApi and
`extractor._build_lookup` prefers the direct row. Stocks and Codal are unchanged, because
they cannot be reached — see the egress section.

The live loop now attempts the direct board first and calls BrsApi only when that board is
incomplete or unavailable. This makes the migration save paid requests instead of merely
preferring one response after spending both quotas.

| Lane | Was | Now | Metered? |
|---|---|---|---|
| Gold / coin / FX | `Market/Gold_Currency*.php` | **TGJU** | no |
| Crypto | `Market/Cryptocurrency.php` | **Wallex** (+ Nobitex cross-check) | no |
| TEDPIX history | `Tsetmc/Index.php` (live only) | **TGJU `bourse` history** | no |
| Commodity | `Market/Commodity.php` | unchanged — **BrsApi fallback** | yes |
| Stocks | `Tsetmc/*.php` | unchanged — **blocked** | yes |
| Codal | `Codal/Announcement.php` | unchanged — **blocked** | yes |

Gold/FX historical backfills now try the mapped TGJU series first and fall back per symbol to
BrsApi when TGJU is unavailable or has no mapped history. The fallback is retained for
coverage rather than silently marking a state complete with an empty direct response.

Verify on any host with `python manage.py check_egress [--compare] [--verify-tsetmc]`.

### Crypto history backfilled (2026-09-01)

`python manage.py backfill_crypto_history` wrote **75,257 rows across 41 pairs**
in one pass, unmetered. Result:

| | Before | After |
|---|---|---|
| Warehouse symbols | 38 | **77** |
| Analysable catalog instruments | 38 | **53** |
| Crypto series | 2 (BTC, USDT_IRT) | **17** |
| BTC depth | 1,037 sessions | **2,491** (to 1398-07-23) |
| USDT/Toman depth | 1,035 sessions | **2,756** (to 1397-09-06) |

ETH, XRP, ADA, DOGE, LTC, BNB, SOL, TRX, DOT, AVAX, LINK, SHIB, UNI, ATOM and
FIL had **no history at all** before this, so none of them could enter the
returns matrix — invisible to the optimizer, comparison and risk regardless of
whether anyone held them.

The ingest is **insert-only**, and the reason is measured rather than cautious:
across 1,037 overlapping BTC days Wallex and the incumbent agreed to a median
0.43% (p90 1.42%), and on USDT/Toman to a median 0.26%. Close enough to trust
for days we lack; not close enough to restate days we have, which would stitch
two venues into one series and leave a discontinuity at every join in a table
the returns matrix reads. Verified after the run: `BTC 1405-06-09` still holds
the incumbent's 78,841.

### The discovery that made the gold/FX swap trivial

**BrsApi's gold/currency feed is TGJU, resold.** Compared against what production was
serving at the same instant, five of eight assets matched *to the rial*:

| Asset | BrsApi (Toman) | TGJU (Rial ÷ 10) | Δ |
|---|---|---|---|
| `emami_coin` | 223,510,000 | 223,510,000 | **exact** |
| `half_coin` | 114,000,000 | 114,000,000 | **exact** |
| `quarter_coin` | 61,500,000 | 61,500,000 | **exact** |
| `one_gram_coin` | 32,000,000 | 32,000,000 | **exact** |
| `euro_cash` | 243,980 | 243,980 | **exact** |
| `usd_cash` | 209,300 | 209,295 | −0.002% |
| `gold_18k_gram` | 22,220,100 | 22,183,800 | −0.16% |
| `usdt_irt` | 209,053 | 209,643 | +0.28% |

The three inexact rows differ by one refresh interval, not by content. This is not a
substitute feed at reduced quality — it is the same feed, one hop earlier, unmetered.

### Two traps in the new sources, both encoded in code rather than trusted to prose

**TGJU declares no unit.** Every other provider sends a unit string, and
`currency.to_toman` is built to trust it — the project rule is that currency is *declared*,
never inferred from magnitude. TGJU breaks that, so `tgju.SLUG_UNITS` is a hand-verified
map. A magnitude heuristic cannot distinguish a unit error from a price move, and Nobitex
quotes Rial where Wallex quotes Toman, so the two differ by exactly 10× on the same coin.

**TGJU serves dead slugs alongside live ones, with nothing marking which.** `usdt-irr` —
the obvious slug to reach for — still returns **273,000, stamped 2020-11-11**. Choosing it
would have valued every USDT holding at roughly a sixth. The live one is
`crypto-tether-irr`. `MAX_QUOTE_AGE` is therefore load-bearing, not hygiene, and
`tests/test_direct_sources.py` pins it against the real captured payload.

## The finding that reorders everything: the VPS is in Frankfurt

```
$ curl https://ipinfo.io/json
{"ip":"89.106.206.4","city":"Frankfurt am Main","country":"DE","org":"AS202269 BitCommand LLC"}
```

This is why `codal.ir` times out, and it is why the TSETMC plan below is not available. It
is a single fact that decides the whole data-source strategy, and it was not visible from
any amount of reading endpoint documentation.

## What is actually reachable

Every host below resolves; the difference is whether TCP 443 completes.

| Host | DNS | TCP 443 | HTTP | Verdict |
|---|---|---|---|---|
| `cdn.tsetmc.com` | ✓ 3 IPs | **timeout** | — | **blocked** |
| `www.tsetmc.com` · `old.` · `main.` · `service.` | ✓ | **timeout** | — | **blocked, every hostname** |
| `cdn.tsetmc.com:80` | ✓ | **timeout** | — | blocked on plain HTTP too |
| `codal.ir` | ✓ | **timeout** | — | blocked (already known) |
| `api.nobitex.ir` | **NXDOMAIN** | — | — | wrong hostname |
| **`apiv2.nobitex.ir`** | ✓ | **open** | **200** | **works** |
| **`api.wallex.ir`** | ✓ | **open** | **200** | **works** |
| **`call1.tgju.org` · `api.tgju.org`** | ✓ | **open** | **200** | **works** |
| `api.bitpin.ir` | ✓ | open | 200 | works |
| `Api.BrsApi.ir` | ✓ | open | TLS 0.24s | works (current provider) |

**TSETMC is blocked by TSETMC, not by the VPS.** Every other Iranian-hosted service —
BrsApi, Wallex, TGJU, Bitpin, Nobitex — answers from this same machine in under a second.
The exchange geo-blocks foreign egress, on every hostname and on both ports. No amount of
`User-Agent` or `Referer` tuning changes a connection that never completes.

### Correction 1 — Nobitex is reachable; the documented hostname is not

The published docs use `api.nobitex.ir`, which returns **NXDOMAIN** from this VPS.
`apiv2.nobitex.ir` resolves and serves the same API:

```
apiv2.nobitex.ir/market/udf/history?symbol=BTCIRT&resolution=D   200, {"s":"ok",...}
apiv2.nobitex.ir/v2/orderbook/USDTIRT                            200, lastTradePrice 2,086,300
apiv2.nobitex.ir/market/stats?srcCurrency=usdt&dstCurrency=rls   200, bestSell/bestBuy/latest
```

The earlier draft recorded Nobitex as blocked. It is not — it was a DNS-name error.

### Correction 2 — the order book is reachable, just not from TSETMC

The Q7 annex identified the missing order book as the hard ceiling on the limit-to-limit
model, and the previous draft proposed `cdn.tsetmc.com/api/BestLimits` as the fix. **That
endpoint is unreachable from this server.** For *crypto* the order book is available
(Wallex `/v1/depth`, Nobitex `/v2/orderbook`), but for **TSE equities — which is what the
strategy is about — it remains out of reach.** Q7's ceiling stands until there is an Iranian
egress path. The v1 staging in the annex is unaffected: it needs no order book.

---

## Wallex — the strongest reachable source, and it was not on the original list

`https://api.wallex.ir`, no key, no auth for market data. Measured:

| Endpoint | Result |
|---|---|
| `/v1/markets` | 200, 460 KB, **385 symbols** (193 TMN-quoted, 192 USDT-quoted), fa/en names, precision, min notional |
| `/v1/depth?symbol=BTCTMN` | 200, full order book — price / quantity / cumulative sum per level |
| `/v1/trades?symbol=BTCTMN` | 200, recent trades with `isBuyOrder` and timestamp |
| `/v1/udf/history?symbol=&resolution=&from=&to=` | 200, TradingView UDF OHLCV |

**History depth, measured:** `USDTTMN` daily returns **2,755 candles from 2018-11-27 to
2026-08-31** — today, closing at 208,909 Toman. That is ~7.75 years of a second, independent
USD/Toman series.

**Resolutions:** `1`, `15`, `60`, `240`, `D` return `s=ok`. `5`, `180`, `W`, `M` return
`error` — so the supported set is narrower than TradingView's convention suggests and should
be pinned in the endpoint registry rather than assumed.

**No 500-candle cap.** A single 1-minute request returned **16,667 candles**; Nobitex
documents a 500-candle limit and requires paging. Wallex is materially cheaper to backfill.

**Rate limits:** no `RateLimit-*` headers, no documented quota. Measured **60/60 requests
returning 200 in 17.1 s (~3.5 req/s) with zero throttling**, and a separate 30-request burst
with zero rejections. Treat as generous but ungoverned — self-impose a limit rather than
discovering theirs in production.

**Coverage that matters here:** `USDTTMN`, `BTCTMN`, `ETHTMN`, `TRXTMN`, `DOGETMN`, and —
usefully — **`PAXGTMN`, `PAXGUSDT`, `XAUTTMN`, `XAUTUSDT`**. Tokenised gold quoted in Toman
is an independent cross-check on the gold prices the app currently takes from BrsApi on
trust.

## TGJU — a complete, deeper replacement for the gold/FX half

`call1.tgju.org/ajax.json` returns **962 live instruments in one 30 KB request**, timestamped
to the second. Measured at 18:33 Tehran:

| Key | Instrument | Value |
|---|---|---|
| `price_dollar_rl` | USD | 2,094,000 |
| `price_eur` | EUR | 2,438,800 |
| `sekee` / `sekeb` | Emami / Bahar coin | 2,235,100,000 / 2,183,100,000 |
| `nim` / `rob` / `gerami` | half / quarter / gram coin | 1,140,000,000 / 615,000,000 / 320,000,000 |
| `geram18` / `geram24` | 18k / 24k gold per gram | 222,044,000 / 296,056,000 |
| `mesghal` | mesghal | 961,770,000 |
| `ons` / `silver` | gold / silver ounce (USD) | 4,433.80 / 66.31 |

And `api.tgju.org/v1/market/indicator/summary-table-data/{key}` gives daily history:

| Instrument | Records | From |
|---|---|---|
| `price_dollar_rl` | 3,938 | **2011-11-26** |
| `sekee` | 4,276 | **2010-04-04** |
| `geram18` | 3,490 | **2013-07-22** |

That is deeper than BrsApi's `history=2` (back to 1390 ≈ 2011) for coins, free, and keyless.

## Revised recommendation

**Use TGJU first for mapped gold/FX live and historical data, with BrsApi as a
coverage fallback.** The live loop suppresses the paid market request when the
direct board is complete, and the archive chooses TGJU per symbol before using
the paid full-history endpoint. This preserves recovery coverage without paying
for both successful responses.

**Use Wallex as the primary crypto source, Nobitex (`apiv2`) as the cross-check.** This
closes the gap named in the evaluation: crypto currently sits in
`_RETIRED_ARCHIVE_ENDPOINTS` (`archive.py:169`) with no provider history at all, so
`MarketDailyBar` is distilled from live polling and a crypto position cannot be back-dated
before this deployment started watching. Wallex gives ~7.75 years of real OHLC, and two
independent USDT/IRT series give the `usd_cash` rate — which `_dollar_quotes_to_toman`
multiplies through every dollar-quoted asset — a corroborating source for the first time.

**TSETMC is blocked on transport, exactly as Codal is.** This is now a networking decision,
not an engineering one, and it is the same decision `CODAL_HTTP_PROXY` is already waiting on:

- **An Iranian egress hop** — a small VPS inside Iran running a forward proxy, with the
  Frankfurt box dialling through it. Unblocks TSETMC *and* Codal together, which is the
  argument for doing it: two subsystems, one fix. `CODAL_HTTP_PROXY` already exists as the
  configuration point.
- **Move the whole deployment into Iran.** Larger change; also puts the app next to every
  data source it uses.
- **Stay on BrsApi's TSETMC plan.** Legitimate. It costs the ~2-year tick backfill and the
  TSE order book, and those are the two things the migration was for.

Until one of those happens, the ~5M-request tick backlog stays governed by a 10,000/day
wallet, `INTEGRITY_FAILURE_RATE_THRESHOLD = 0.85` stays a permanent condition rather than a
transient one, and Q7 stays capped at what executed ticks can show.

### Home-network egress

An Iranian home computer or router can serve as the egress node. It must have an Iranian
public source address and remain reachable from the application VPS; a home connection
outside Iran does not change the geo-block. Run `scripts/setup_iran_egress.sh server` on an
always-on Linux host (or a router with WireGuard/tinyproxy support), forward UDP 51820 to it
if the ISP permits inbound traffic, and run the `client` mode on the application VPS.
`AllowedIPs` is intentionally limited to the tunnel subnet, so only proxy traffic crosses
the tunnel and SSH/deploy traffic keeps its normal route. If the home ISP uses CGNAT and
cannot forward UDP, a small public relay is required; it must not be an open proxy.

For a Mac behind CGNAT, the simpler option is a private Tailscale link:

1. Join the Mac and application VPS to the same tailnet.
2. Run a localhost-only HTTP proxy on the Mac, then expose that port to the tailnet with
   `tailscale serve --tcp=8888 tcp://localhost:8888`.
3. Set `IRAN_EGRESS_PROXY=http://<mac-tailscale-ip>:8888` on the application deployment.
4. Run `python manage.py check_egress --verify-tsetmc` from the deployed backend before
   enabling direct TSETMC or Codal jobs.

This keeps the proxy off the public internet and does not require inbound access to the
home router. The Mac must stay awake and connected; losing it activates the existing
reachability breaker and paid-provider fallback rather than returning empty market data.

## Suggested sequencing

1. **Wallex crypto history.** Reachable, keyless, deep, no schema change beyond writing
   `MarketCandle` / `MarketDailyBar` rows. Repairs a named Q2/Q3 gap. Start here.
2. **TGJU alongside BrsApi**, writing to the same gold/FX tables and diffing. A free
   correctness audit of the provider currently trusted, and the ingest path is already
   idempotent (`bulk_create(ignore_conflicts=True)`).
3. **Nobitex `apiv2` as the second crypto opinion**, feeding the same validation gate.
4. **Decide the Iranian-egress question.** Everything TSETMC — ticks at a sane rate, the
   TSE order book, Q7 v2, and Codal — is downstream of it.

## What changes in the code

The architecture is already shaped for this: one registry, one quota module, one ingest
layer.

- **`endpoints.py`** — `REGISTRY` already declares nature, bucket and billing plan per
  endpoint. Add a `provider` field beside `plan` (`brsapi` / `wallex` / `nobitex` / `tgju`).
  Everything downstream routes off this registry, which is why `fetchers._call` stays generic.
- **`quota.py`** — the real design change. `ApiRequestQuota` models a *purchased daily
  wallet*; a keyless source has a *rate ceiling* instead. Keep the wallet for BrsApi (the
  provider's own `account` block reconciles it) and add a Redis token bucket for the keyless
  providers, tripping the breaker on 429/403 rather than on a body-sniffed quota message.
  `reserve_request` keeps its signature; only the governor behind it changes.
- **`ingest.py`** — one adapter per endpoint, writing into the **existing** tables. Wallex
  UDF is column-major (`{s,t,o,h,l,c,v}`) rather than row-major, which is a genuinely
  different shape from every current ingest path.
- **`validation.py`** — `reconcile_tick_volume` already cross-checks a day's ticks against
  the candle. With two crypto sources it can do the same across providers, which is the
  cheapest possible use of the redundancy.

## Reproducing this

```bash
ssh root@89.106.206.4 'bash -s' <<'EOF'
for h in cdn.tsetmc.com www.tsetmc.com apiv2.nobitex.ir api.wallex.ir call1.tgju.org codal.ir; do
  printf '%-22s ' "$h"
  timeout 8 bash -c "cat < /dev/null > /dev/tcp/$h/443" 2>/dev/null && echo OPEN || echo BLOCKED
done
curl -s -m 20 "https://api.wallex.ir/v1/udf/history?symbol=USDTTMN&resolution=D&from=0&to=$(date +%s)" \
  | python3 -c "import sys,json,datetime;d=json.load(sys.stdin);t=d['t'];f=lambda x:datetime.datetime.fromtimestamp(x,datetime.UTC).date();print(len(t),'candles',f(t[0]),'->',f(t[-1]))"
EOF
```

## Sources

- [Wallex API docs](https://developers.wallex.ir/docs) — endpoint shapes confirmed by direct probe
- [Nobitex API docs](https://apidocs.nobitex.ir/) — note the docs say `api.nobitex.ir`; use `apiv2.nobitex.ir`
- [m-ahmadi/exref — TSETMC endpoint catalog](https://github.com/m-ahmadi/exref/blob/master/tse/urls.txt) — accurate, but unreachable from this VPS
- [mahs4d/tsetmc-api](https://github.com/mahs4d/tsetmc-api) · [tse-client](https://github.com/m-ahmadi/tse-client)
- [Abantether docs](https://docs.abantether.com/) — OTC only, no candles, no order book; **do not integrate**

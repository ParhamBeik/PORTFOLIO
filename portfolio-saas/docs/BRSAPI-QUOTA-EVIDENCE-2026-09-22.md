# BrsApi quota billing evidence — 2026-09-22 / 1405-06-31

Source: signed-in BrsApi panel (`https://api.brsapi.ir/Panel/panel.html`) and
production Django quota admin. All counts are **HTTP requests**, not tokens.
The pre-reset comparison alone did not prove endpoint billing; the isolated
counter experiment below did so for the tested endpoints the following day.

| 2026-09-22 meter | BrsApi panel | Local old-plan row | Difference |
|---|---:|---:|---:|
| AIO (All In One) / TSETMC | 10,023 | 9,848 | +175 |
| Market CGCC / BRS | 761 | 936 | −175 |

The old Market row's `archive_used=169` and `other_used=6` sum to 175.
That equality suggests those calls were charged to AIO while the application
assigned them to Market. The isolated matrix below confirms the product rule
for the tested endpoints, but cannot retrospectively identify every one of
the 175 historical calls.

## Isolated counter matrix, 2026-09-23 / 1405-07-01

The task resumed after the midnight window. This experiment ran around
10:43–10:47 Tehran, **not immediately after reset**. The production scheduler
and three provider workers were stopped; the panel was stable at 9,181 AIO
and 354 Market. Each request used the existing production key inside the
backend container, and the panel was refreshed after each request. No key or
response body was logged. Workers and scheduler were restarted and healthy
afterward. Eight raw GETs were issued; six returned 200, one 400, one 403.

| Case | Endpoint and parameters | Before AIO / Market | After AIO / Market | HTTP / response | Inferred billed product |
|---|---|---|---|---|---|
| No browser headers | `Tsetmc/Index.php?type=1` | 9181 / 354 | 9181 / 354 | 403 / 1,284 bytes | Not billed |
| TSETMC | `Tsetmc/Index.php?type=1` | 9181 / 354 | 9182 / 354 | 200 / 248 bytes | AIO +1 |
| Codal | `Codal/Announcement.php?page=1` | 9182 / 354 | 9183 / 354 | 200 / 14,260 bytes | AIO +1 |
| Market live | `Market/Gold_Currency.php` | 9183 / 354 | 9183 / 355 | 200 / 14,670 bytes | Market CGCC +1 |
| Pro history 1 | `Market/Gold_Currency_Pro.php?symbol=USD&history=1` | 9183 / 355 | 9184 / 355 | 200 / 58,432 bytes | AIO +1 |
| Pro history 2 | `Market/Gold_Currency_Pro.php?symbol=USD&history=2` | 9184 / 355 | 9185 / 355 | 200 / 370,667 bytes | AIO +1 |
| Invalid Pro section | `Market/Gold_Currency_Pro.php?section=1` | 9185 / 355 | 9186 / 355 | 400 / 223 bytes | AIO +1 despite error |
| Pro default | `Market/Gold_Currency_Pro.php` | 9186 / 355 | 9187 / 355 | 200 / 32,747 bytes | AIO +1 |

The first request lacked the application's normal browser-like request headers
and was rejected before billing. All later requests used the same headers as
the deployed fetcher. The successful `Gold_Currency_Pro` modes all debit AIO;
the earlier assumption that `history=1` debits Market CGCC was false. A 400
provider response **can still debit AIO**, so attempts and successes must be
separate counters. The provider panel confirms a 10,000-request AIO limit and
a 1,500-request Market CGCC limit for this account.

## Complete registered-path matrix, 2026-09-23

A second VPS-origin experiment covered **all 13 distinct paths** in the current
endpoint registry. The production scheduler and three provider workers were
gracefully stopped until their containers exited. The signed-in provider panel
was refreshed after every request. Each request used the production backend
container's configured key and its normal browser-like headers; only HTTP
status and response length were printed. No key, URL query, or response body
was logged. Both counters stayed fixed at **AIO 9,466 / Market 636** before
the first request. Two local probe-script argument failures sent no HTTP
request and moved neither counter; the script was corrected before testing.

| Registered endpoint / variant | HTTP | AIO after | Market after | Billed |
|---|---:|---:|---:|---|
| `Tsetmc/Option.php` | 200 | 9,467 | 636 | AIO +1 |
| `Tsetmc/Index.php?type=1` | 200 | 9,468 | 636 | AIO +1 |
| `Tsetmc/Symbol.php?l18=…` | 200 | 9,469 | 636 | AIO +1 |
| `Tsetmc/AllSymbols.php` | 200 | 9,470 | 636 | AIO +1 |
| `Tsetmc/History.php?l18=…&type=0` | 200 | 9,471 | 636 | AIO +1 |
| `Tsetmc/Candlestick.php?l18=…&type=3` | 200 | 9,472 | 636 | AIO +1 |
| `Tsetmc/Transaction.php?l18=…&date=1405-06-31` | 200 | 9,473 | 636 | AIO +1 |
| `Tsetmc/Shareholder.php?l18=…` | 200 | 9,474 | 636 | AIO +1 |
| `Codal/Announcement.php?page=1` | 200 | 9,475 | 636 | AIO +1 |
| `Market/Gold_Currency.php` | 200 | 9,475 | 637 | Market +1 |
| `Market/Gold_Currency_Pro.php` | 200 | 9,476 | 637 | AIO +1 |
| `Market/Cryptocurrency.php` | 200 | 9,476 | 638 | Market +1 |
| `Market/Commodity.php` | 200 | 9,476 | 639 | Market +1 |
| Invalid `Tsetmc/Index.php?type=999` | 400 | 9,477 | 639 | AIO +1 |
| Invalid `Market/Gold_Currency_Pro.php?section=1` | 400 | 9,478 | 639 | AIO +1 |

The previous matrix separately covered `Gold_Currency_Pro` history modes 1
and 2, both AIO +1, and a headerless 403 that did **not** move either meter.
These results prove billing for the listed request forms, not every possible
invalid parameter or every future provider behavior. All four stopped
containers were restarted; the three workers returned healthy and beat was
running. Neither experiment ran at the exact Tehran-midnight reset.

The panel is still the provider's authority; local request counting alone
cannot equal its number when any manual request, retry, or second client uses
the same key. The optional account-panel reader is disabled until a VPS-origin
credentialed read is verified. Until then, Operations must label provider
observation as unavailable rather than present its safe-budget estimate as a
provider total.

A read-only VPS request to the panel overview with the configured API key but
without a `Phone` parameter returned HTTP 400 / `successful=false`, with no
meters. The saved browser session displays counters but does not expose the
account phone in its visible UI. The reader therefore remains disabled until
the account phone is supplied and a successful VPS read is checked for whether
it changes either meter.

## Signed-in browser follow-up, 2026-09-23 about 13:00 UTC

The account panel remained accessible through the user's authenticated in-app
browser. A refresh showed **AIO 10,002/10,000** and **Market CGCC 755/1,500**.
The deployed Django quota admin, refreshed immediately before the panel,
showed the legacy product rows for September 23 as `tsetmc used=9,835`
(archive 9,437, live 347, other 51; last update 12:55 p.m. in admin) and
`brs used=900` (archive 149, live 751, other 0; last update 12:58 p.m.).
Thus the visible differences at that moment were +167 AIO and −145 Market,
but the timestamps are not identical and prior manual probes used the same
key. These figures must **not** be called a clean product reconciliation.
No further AIO endpoint probes were sent after observing the provider meter
above its 10,000 daily limit. The browser session is sufficient for manual
before/after proof; it is not a credential source available to the unattended
VPS worker after deployment.

## VPS panel-reader proof, 2026-09-24

The account phone was placed only in the VPS `.env.production` (mode 600), not
in Git. A VPS-origin HTTPS request to the panel overview returned HTTP 200,
`successful=true`, with AIO `9,943/10,000` and Market CGCC `364/1,500`.
The scheduler and three provider workers were then stopped. Two consecutive
authenticated panel reads returned those same counters without movement; no
market-data endpoint request was made during this check. All stopped workers
and the scheduler were restarted, with live and archive workers healthy.
This proves that these two panel reads were not billed; it does not guarantee
future provider behavior. The VPS-only enable flag is staged for the next
deployment. New product rows now refuse requests until a provider observation
seeds the admission counter, preventing a mid-day rename from creating a fresh
zero-usage wallet. Local attribution remains distinct from the provider total.

## Production activation, 2026-09-24

The opt-in panel meter was enabled in the VPS-only environment and deployed.
The first observed product totals were AIO 9,977/10,000 and Market CGCC
610/1,500. They are exact provider-reported daily request totals, but the new
product rows began after the Tehran day started. Usage before that observation
cannot be assigned to local `archive`, `live`, or `other` buckets from these
rows. The Operations view now identifies it as a separate, immutable
pre-observation baseline. Signed variance is computed only on the subsequent
provider delta versus subsequent locally attributed attempts. The full daily
provider total is never overwritten with a local estimate or forced to match.

The first fully comparable day is the next Tehran-day reset. A genuine
post-baseline variance must remain visible and be investigated; the mid-day
baseline is not proof that all application calls reconcile exactly.

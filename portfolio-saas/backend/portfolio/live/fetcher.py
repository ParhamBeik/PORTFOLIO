"""Live market fetch layer for the 2-minute price loop (BRS + TSETMC).

Historical/warehouse fetchers live in `marketdata.fetchers`.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from django.conf import settings

from marketdata.fetchers import (
    MarketDataFetchError,
    fetch_gold_currency_pro_history_24h,
    fetch_json,
)
from marketdata.quota import BRS, LIVE, TSETMC, QuotaExhausted
from portfolio.live import find_symbol_record
from datetime import datetime
from django.core.cache import cache
from marketdata import ingest
from marketdata.sources import nobitex, tgju, wallex
from marketdata.sources.http import SourceError
from marketdata.workflows import submit_with_context
from portfolio.models import Asset
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 6.1; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
}

KAMA_SYMBOL = "کاما"

#: Per-process fallback clock for the paid-board verification cadence, used only
#: when the shared Redis claim is unavailable. See `_brs_verification_due`.
_BRS_VERIFY_LOCAL = {"at": None}

__all__ = [
    "fetch_brsapi",
    "fetch_tsetmc",
    "fetch_tsetmc_symbol",
    "fetch_all_markets",
    "api_settings_from_django",
]


def _extract_price(record):
    """Return a positive TSETMC price from one record, preferring last price."""
    if not isinstance(record, dict):
        return 0
    for field_name in ("pl", "pc"):
        try:
            price = float(record.get(field_name) or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return 0


def _find_symbol_record(tsetmc_payload, symbol):
    if tsetmc_payload is None:
        # See extractor._find_tsetmc_symbol: an absent payload is a closed
        # market, not an unresolvable symbol.
        logger.debug("No TSETMC payload this cycle (market closed); %s unchanged.", symbol)
        return None
    record = find_symbol_record(tsetmc_payload, symbol)
    if record is None:
        logger.warning("No exact TSETMC match for %s; skipping.", symbol)
    return record


def fetch_brsapi(brs_url, brs_api_key):
    try:
        data = fetch_json(
            brs_url,
            params={"key": brs_api_key},
            headers=HEADERS,
            quota_bucket=LIVE,
            quota_plan=BRS,
        )
        logger.info("Successfully fetched gold/currency payload from %s", brs_url)
        return data
    except MarketDataFetchError as exc:
        logger.error("Failed to fetch BRS data from %s: %s", brs_url, exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error fetching BRS data: %s", exc)
        return None


def fetch_tsetmc(tsetmc_url, tsetmc_api_key):
    try:
        data = fetch_json(
            tsetmc_url,
            params={"key": tsetmc_api_key, "type": "1"},
            headers=HEADERS,
            quota_bucket=LIVE,
            quota_plan=TSETMC,
        )
        logger.info("Successfully fetched stock payload from %s", tsetmc_url)
        return data
    except MarketDataFetchError as exc:
        logger.error("Failed to fetch TSETMC data from %s: %s", tsetmc_url, exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error fetching TSETMC data: %s", exc)
        return None


def fetch_tsetmc_symbol(tsetmc_symbol_url, tsetmc_api_key, symbol):
    try:
        data = fetch_json(
            tsetmc_symbol_url,
            params={"key": tsetmc_api_key, "l18": symbol},
            headers=HEADERS,
            quota_bucket=LIVE,
            quota_plan=TSETMC,
        )
        logger.info("Fetched symbol %s from %s", symbol, tsetmc_symbol_url)
        return data
    except QuotaExhausted as exc:
        logger.warning("Quota exhausted for symbol fetch %s: %s", symbol, exc)
        return None
    except MarketDataFetchError as exc:
        logger.error("Failed symbol fetch for %s: %s", symbol, exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error fetching symbol %s: %s", symbol, exc)
        return None


# Both provider jobs are I/O-bound HTTP calls (requests, not asyncio elsewhere in
# this sync Django/Celery codebase), so a thread pool is enough to run them
# concurrently. Worst case per job is (retries+1)*timeout + backoff sleeps ~= 63s;
# sequential BRS+TSETMC could approach the 2-minute Celery beat interval, risking
# overlapping task runs. FETCH_TIMEOUT bounds the combined wait per job.
FETCH_TIMEOUT = 90


def _usdt_irt_quote(aio_key):
    """The USDT/IRT fallback quote, cached so it is not bought every cycle.

    This is a *fallback*: `extractor._lookup_usdt_toman` only reaches for it when
    the main board echoed the USD peg instead of the tether rate, and
    `_overlay_usdt_irt_from_warehouse` can still cover it after that. Buying it
    on every single cycle made a rarely-read fallback the second most expensive
    thing on the Market/* meter -- at the tightened cadence it would have been
    ~900 AIO requests/day, more than the live board itself.

    Cached in the Django cache (Redis in every deployed environment), so the
    saving is shared across worker processes rather than per-process. A cache
    miss just buys it again; there is no correctness dependency on the TTL.
    """

    ttl = int(getattr(settings, "MARKETDATA_USDT_QUOTE_TTL_SECONDS", 600) or 0)
    key = "marketdata:usdt_irt_quote"
    if ttl > 0:
        try:
            cached = cache.get(key)
            if cached is not None:
                return cached
        except Exception:
            pass  # Cache down is not a reason to skip the quote.
    quote = fetch_gold_currency_pro_history_24h(aio_key, "USDT")
    if quote and ttl > 0:
        try:
            cache.set(key, quote, timeout=ttl)
        except Exception:
            pass
    return quote


def _brs_job(brs_url, brs_key, aio_key):
    result = {"brsapi": fetch_brsapi(brs_url, brs_key)}
    if aio_key:
        try:
            usdt_quote = _usdt_irt_quote(aio_key)
            if usdt_quote:
                result["usdt_irt_quote"] = usdt_quote
        except Exception as exc:
            logger.warning("Could not fetch USDT/IRT quote: %s", exc)
    return result


def _required_symbols():
    """Every BrsApi symbol some active asset is priced by, upper-cased.

    This is the bar the free sources must clear before the paid call is worth
    skipping. Read live rather than cached: a user adding an asset from the
    market catalog changes the answer immediately, and pricing their new
    holding matters more than saving one request.
    """

    return {
        symbol.upper()
        for symbol in Asset.objects.filter(is_active=True)
        .exclude(brs_symbol="")
        .values_list("brs_symbol", flat=True)
        if symbol
    }


def _direct_job():
    """Gold/FX from TGJU and crypto from Wallex -- the unmetered origins.

    This runs before the BrsApi job. The extractor still prefers whichever
    produced a row (see `extractor._build_lookup`), while the caller skips the
    paid fallback when the direct board is complete. That ordering makes the
    migration save quota rather than merely preferring one response after both
    requests were already spent.

    Failures are logged and swallowed. A free origin going down must not take
    the price loop with it -- the paid one is still there, which is precisely
    the property that makes running both worth the extra request.
    """

    result = {}
    rows = []

    if getattr(settings, "TGJU_ENABLED", False):
        try:
            rows.extend(tgju.live_rows())
        except SourceError as exc:
            logger.warning("TGJU live fetch failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected TGJU failure: %s", exc)

    if getattr(settings, "WALLEX_ENABLED", False):
        try:
            wallex_rows = wallex.live_rows()
            result["wallex_rows"] = wallex_rows
            # Only the Toman book feeds pricing. The USDT book quotes coins in
            # tether, and mixing the two under one symbol is a ~200,000x error.
            rows.extend(
                {"symbol": r["base"], "price": r["price"], "unit": r["unit"]}
                for r in wallex_rows
                if r.get("quote") == "TMN" and r.get("base")
            )
        except SourceError as exc:
            logger.warning("Wallex live fetch failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected Wallex failure: %s", exc)

    if rows:
        # Shaped like a BrsApi envelope so `_build_lookup` consumes it unchanged.
        result["direct"] = {"rows": rows}
        # Whether the paid market request would add anything. Measured against
        # the symbols the APP needs -- every active asset's `brs_symbol` -- not
        # against `tgju.BRS_TO_SLUG`.
        #
        # Those two sets happen to coincide today (all 8 priced assets are
        # mapped), which is exactly what makes the distinction easy to miss. But
        # the market catalog offers ~5,000 BrsApi instruments a user can add,
        # and the moment someone adds one TGJU does not carry -- GBP, or
        # IR_COIN_BAHAR -- keying on the mapping would declare the board
        # complete, skip the only source that quotes their holding, and leave it
        # frozen with no error anywhere.
        direct_symbols = {str(row.get("symbol", "")).upper() for row in rows}
        result["direct_complete"] = bool(direct_symbols) and _required_symbols() <= direct_symbols
        logger.info("Direct sources supplied %s live rows.", len(rows))

    # Cross-check is advisory: it never changes a price, it only reports when
    # two exchanges disagree by more than a spread. Cheap, because both payloads
    # are already in hand.
    if getattr(settings, "NOBITEX_ENABLED", False) and result.get("wallex_rows"):
        try:
            _, disagreements = nobitex.cross_check(
                nobitex.live_rows(), result["wallex_rows"]
            )
            for row in disagreements:
                logger.warning(
                    "crypto_source_disagreement coin=%s nobitex=%s wallex=%s spread=%.2f%%",
                    row["coin"], row["nobitex_toman"], row["wallex_toman"],
                    row["spread"] * 100,
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("Crypto cross-check unavailable: %s", exc)

    return result


def _blend_paid_board():
    """Whether to buy the paid gold/FX board on every cycle, not just as a fallback.

    At the 60/90/180s cadences this is ~890 board requests on a trading day.
    Market CGCC now has a 1,500-request daily limit, and the rolling window
    also shapes bursts.
    """
    return bool(getattr(settings, "MARKETDATA_BLEND_PAID_BOARD", False))


def _brs_verification_due():
    """Whether to buy the paid board even though the free origins look complete.

    `direct_complete` skipping BrsApi entirely is the right default and is most
    of why Market/* call volume stayed low -- but taken alone it means nothing
    ever contradicts TGJU. That feed is known to keep answering on slugs that
    have stopped updating, and a stale-but-answering slug satisfies
    `direct_complete` exactly as well as a live one does. So a frozen price would
    look healthy indefinitely, on the one board we have no second opinion for.

    One provider board per interval fixes that for ~96 requests/day. Claimed with
    Redis SET NX EX so several workers on the same cycle buy it once between
    them, not once each; if Redis is down we fall back to buying it, because the
    fallback that spends a little is safer than the one that goes blind.
    """
    interval = int(getattr(settings, "MARKETDATA_BRS_VERIFY_INTERVAL_SECONDS", 0) or 0)
    if interval <= 0:
        return False
    from portfolio.live.redis_client import get_redis

    client = get_redis()
    if client is not None:
        try:
            return bool(client.set("marketdata:brs_verify", "1", ex=interval, nx=True))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not claim the BRS verification slot: %s", exc)

    # Degraded: throttle per process instead of not at all. Returning True here
    # unconditionally would buy a provider board on EVERY cycle for as long as
    # Redis was down -- ~900 extra requests/day at the current cadence. A
    # per-process clock over-spends by at most the worker
    # count, which is bounded; "always" is not.
    # "Never claimed" has to be None, not 0.0. `time.monotonic()` counts from
    # boot, not from the epoch, so on a machine that came up less than
    # `interval` ago the arithmetic reads a zero sentinel as "claimed seconds
    # ago" and suppresses the board for the first fifteen minutes of uptime --
    # precisely the window in which a worker is most likely to be restarting
    # because something was already wrong.
    last = _BRS_VERIFY_LOCAL.get("at")
    now = time.monotonic()
    if last is not None and now - last < interval:
        return False
    _BRS_VERIFY_LOCAL["at"] = now
    return True


def _tsetmc_job(tsetmc_url, tsetmc_key, tsetmc_symbol_url):
    result = {"tsetmc": fetch_tsetmc(tsetmc_url, tsetmc_key)}
    kama_record = _find_symbol_record(result["tsetmc"], KAMA_SYMBOL)
    if _extract_price(kama_record) <= 0:
        cooldown_key = f"cooldown:fallback_fetch:{KAMA_SYMBOL}"
        if cache.get(cooldown_key):
            logger.debug("Fallback fetch for %s is on cooldown, skipping", KAMA_SYMBOL)
        else:
            kama_data = fetch_tsetmc_symbol(tsetmc_symbol_url, tsetmc_key, KAMA_SYMBOL)
            result["tsetmc_symbol_kama"] = kama_data

            # Normalize and extract price to check if the fetch succeeded
            kama_record_fallback = None
            if kama_data:
                if isinstance(kama_data, dict):
                    if kama_data.get("pl") or kama_data.get("pc"):
                        kama_record_fallback = kama_data
                    else:
                        for value in kama_data.values():
                            if isinstance(value, list) and value and isinstance(value[0], dict):
                                kama_record_fallback = value[0]
                                break
                elif isinstance(kama_data, list) and kama_data and isinstance(kama_data[0], dict):
                    kama_record_fallback = kama_data[0]

            if _extract_price(kama_record_fallback) <= 0:
                logger.info("Fallback fetch for %s failed or returned zero price. Setting cooldown for 30 minutes.", KAMA_SYMBOL)
                cache.set(cooldown_key, True, timeout=1800)  # 30-minute cooldown
    return result


def fetch_all_markets(api_settings):
    """Return all raw market payloads needed by extractor.extract_standard_prices.

    All 6 endpoints are fetched in parallel (ThreadPoolExecutor) since they are
    independent HTTP calls; a slow/failing provider must not block the others.

    Jobs go through `submit_with_context`, not a bare `submit`: a pool worker
    starts from a fresh context, so quota attempts billed inside these threads
    were invisible to the workflow ledger that owns them.
    """

    raw_data = {}
    jobs = []

    brs_url = api_settings.get("brs_url")
    brs_key = api_settings.get("brs_api_key")
    tsetmc_url = api_settings.get("tsetmc_url")
    tsetmc_key = api_settings.get("tsetmc_api_key")

    from marketdata.fetchers import fetch_market_index
    from marketdata.market_state import (
        claim_provider_state_probe,
        live_job_keys,
        market_state,
        release_provider_state_probe,
        remember_provider_state,
    )

    ignore_hours = getattr(settings, "MARKETDATA_IGNORE_MARKET_HOURS", False)

    tehran_now = datetime.now(ZoneInfo("Asia/Tehran"))
    index_probe_claimed = claim_provider_state_probe(tehran_now)

    # The index probe is bought for its STATE, not for its number, so BrsApi
    # goes first here even though TGJU is free.
    #
    # `market_state_at` is clock-based -- weekday plus session hours -- and the
    # only thing that can override it is a provider saying "بسته", cached by
    # `remember_provider_state` (which `fetch_market_index` calls and nothing
    # else does). On a weekday public holiday the clock says OPEN, so without
    # that override the TSE stock job runs every two minutes for a whole
    # session against a shut market. Iran has ~20 such holidays a year.
    #
    # The probe is claimed at most once per half hour, so it costs ~14 requests
    # a day out of the ~10,000 TSETMC wallet. Trading a reliable closed-signal
    # for that is a bad deal in the one direction that matters: TSETMC is the
    # binding wallet -- it hit 10,034/10,000 on 2026-08-26 -- and the waste this
    # prevents is measured in thousands of stock fetches, not fourteen.
    index_payload = None
    if index_probe_claimed and tsetmc_url and tsetmc_key:
        try:
            index_payload = fetch_market_index(tsetmc_key)
            if index_payload:
                ingest.ingest_market_index(index_payload)
                raw_data["market_index"] = index_payload
        except Exception as exc:
            release_provider_state_probe()
            logger.warning("Index probe failed: %s", exc)

    # TGJU covers the index only when the paid probe produced nothing -- either
    # it was not claimed this cycle, or it failed. It is a free value, not a
    # second opinion on whether the market is open: see `live_tedpix_payload`,
    # which derives open/closed from the quote's own age rather than asserting it.
    if index_payload is None and getattr(settings, "TGJU_ENABLED", False):
        try:
            direct_index = tgju.live_tedpix_payload(now=tehran_now)
            if direct_index:
                ingest.ingest_market_index(direct_index)
                raw_data.setdefault("market_index", direct_index)
                remember_provider_state(direct_index)
        except Exception as exc:  # noqa: BLE001 - never let a free source break the loop
            logger.warning("TGJU index probe failed: %s", exc)

    current_state = market_state()
    planned = set(live_job_keys(
        state=current_state,
        now=tehran_now,
        has_brs=bool(brs_url and brs_key),
        has_tsetmc=bool(tsetmc_url and tsetmc_key),
        ignore_hours=ignore_hours,
    ))

    executor = ThreadPoolExecutor(max_workers=6)

    # The direct origins are unmetered, so they are gated only on the market
    # being open -- not on a key existing, and not on the BrsApi wallet having
    # anything left in it. That independence is the point: on 2026-08-26 a full
    # TSETMC backfill drained the shared budget before dawn and the USDT quote
    # failed 201 times, freezing every dollar-denominated holding. A source that
    # costs nothing cannot be starved by the archive.
    direct_enabled = any(
        getattr(settings, name, False)
        for name in ("TGJU_ENABLED", "WALLEX_ENABLED", "NOBITEX_ENABLED")
    )
    direct_result = {}
    if direct_enabled and "gold_currency" in planned:
        # Resolve the free origin first. This makes BrsApi a true fallback
        # instead of a parallel request that spends quota even when direct
        # prices are complete.
        direct_result = _direct_job()
        direct_complete = bool(direct_result.pop("direct_complete", False))
        raw_data.update(direct_result)
    else:
        direct_complete = False

    if brs_url and brs_key:
        if "gold_currency" not in planned:
            logger.info("Gold & currency job not scheduled this cycle. Skipping.")
        elif _blend_paid_board() or not direct_complete or _brs_verification_due():
            # Blended mode buys the paid board every cycle alongside the free
            # ones and lets `extractor._build_lookup` prefer whichever answered.
            #
            # The quota argument for skipping it is gone: the Market/* meter is
            # 1,500/DAY and was running at ~42. The remaining arguments are about
            # data quality and they point the other way -- BrsApi declares a unit
            # string on every row where TGJU needs slug mapping, and TGJU is known
            # to keep answering on slugs that have stopped updating, which
            # `direct_complete` cannot distinguish from a live one.
            jobs.append(submit_with_context(executor, _brs_job, brs_url, brs_key, tsetmc_key))
        else:
            logger.info("Direct market sources cover the mapped board; skipping BRS fallback.")
    else:
        logger.warning("BRS API URL or Key missing in Django settings.")

    if tsetmc_url and tsetmc_key:
        if "tsetmc" in planned:
            tsetmc_symbol_url = api_settings.get("tsetmc_symbol_url", settings.TSETMC_SYMBOL_URL)
            jobs.append(submit_with_context(
                executor, _tsetmc_job, tsetmc_url, tsetmc_key, tsetmc_symbol_url
            ))
        else:
            logger.info("Tehran Stock Exchange (TSE) is closed. Skipping stocks.")
    else:
        logger.warning("TSETMC URL or Key missing in Django settings.")

    if jobs:
        done, not_done = wait(jobs, timeout=FETCH_TIMEOUT)
        for job in not_done:
            logger.error("Provider job did not finish within %ss.", FETCH_TIMEOUT)
        for job in done:
            try:
                raw_data.update(job.result())
            except Exception as exc:
                logger.error("Provider job raised: %s", exc)
    executor.shutdown(wait=False)

    return raw_data


def api_settings_from_django() -> dict:
    """Collect the market source config from Django settings/env."""
    return {
        "brs_url": getattr(settings, "BRS_URL", "https://Api.BrsApi.ir/Market/Gold_Currency.php"),
        "brs_api_key": getattr(settings, "BRS_API_KEY", ""),
        "tsetmc_url": getattr(settings, "TSETMC_URL", "https://Api.BrsApi.ir/Tsetmc/Symbol.php"),
        "tsetmc_api_key": getattr(settings, "TSETMC_API_KEY", ""),
        "tsetmc_symbol_url": getattr(settings, "TSETMC_SYMBOL_URL", "https://Api.BrsApi.ir/Tsetmc/Symbol.php"),
    }

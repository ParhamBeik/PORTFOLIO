"""Live market fetch layer for the 2-minute price loop (BRS + TSETMC).

Historical/warehouse fetchers live in `marketdata.fetchers`.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, wait
from django.conf import settings

from marketdata.fetchers import MarketDataFetchError, fetch_json
from marketdata.quota import LIVE, QuotaExhausted
from portfolio.live import find_symbol_record

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 6.1; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
}

KAMA_SYMBOL = "کاما"

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


def _brs_job(brs_url, brs_key):
    result = {"brsapi": fetch_brsapi(brs_url, brs_key)}
    if brs_key:
        try:
            from marketdata.fetchers import fetch_gold_currency_pro_history_24h

            usdt_quote = fetch_gold_currency_pro_history_24h(brs_key, "USDT")
            if usdt_quote:
                result["usdt_irt_quote"] = usdt_quote
        except Exception as exc:
            logger.warning("Could not fetch USDT/IRT quote: %s", exc)
    return result


def _tsetmc_job(tsetmc_url, tsetmc_key, tsetmc_symbol_url):
    from django.core.cache import cache
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
    """
    raw_data = {}
    jobs = []

    brs_url = api_settings.get("brs_url")
    brs_key = api_settings.get("brs_api_key")
    tsetmc_url = api_settings.get("tsetmc_url")
    tsetmc_key = api_settings.get("tsetmc_api_key")

    from datetime import datetime
    from zoneinfo import ZoneInfo
    from marketdata import ingest
    from marketdata.fetchers import fetch_market_index
    from marketdata.market_state import (
        claim_provider_state_probe,
        live_job_keys,
        market_state,
        release_provider_state_probe,
    )

    ignore_hours = getattr(settings, "MARKETDATA_IGNORE_MARKET_HOURS", False)

    tehran_now = datetime.now(ZoneInfo("Asia/Tehran"))
    if tsetmc_url and tsetmc_key and claim_provider_state_probe(tehran_now):
        try:
            index_payload = fetch_market_index(tsetmc_key)
            if index_payload:
                ingest.ingest_market_index(index_payload)
                raw_data["market_index"] = index_payload
        except Exception as exc:
            release_provider_state_probe()
            logger.warning("Index probe failed: %s", exc)

    current_state = market_state()
    planned = set(live_job_keys(
        state=current_state,
        now=tehran_now,
        has_brs=bool(brs_url and brs_key),
        has_tsetmc=bool(tsetmc_url and tsetmc_key),
        ignore_hours=ignore_hours,
    ))

    executor = ThreadPoolExecutor(max_workers=6)
    if brs_url and brs_key:
        if "gold_currency" in planned:
            jobs.append(executor.submit(_brs_job, brs_url, brs_key))
        else:
            logger.info("Domestic gold & currency market closed overnight. Skipping.")
    else:
        logger.warning("BRS API URL or Key missing in Django settings.")

    if tsetmc_url and tsetmc_key:
        if "tsetmc" in planned:
            tsetmc_symbol_url = api_settings.get("tsetmc_symbol_url", settings.TSETMC_SYMBOL_URL)
            jobs.append(executor.submit(_tsetmc_job, tsetmc_url, tsetmc_key, tsetmc_symbol_url))
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

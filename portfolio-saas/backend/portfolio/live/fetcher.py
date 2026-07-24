"""Live market fetch layer for the 2-minute price loop (BRS + TSETMC).

Historical/warehouse fetchers live in `marketdata.fetchers`.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, wait
from django.conf import settings

from marketdata.fetchers.base import MarketDataFetchError, fetch_json
from marketdata.quota import LIVE, QuotaExhausted

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
    if not isinstance(tsetmc_payload, list):
        return None
    normalized_symbol = str(symbol).strip().casefold()
    for record in tsetmc_payload:
        if isinstance(record, dict) and str(record.get("l18", "")).strip().casefold() == normalized_symbol:
            return record
    for record in tsetmc_payload:
        if not isinstance(record, dict):
            continue
        l18 = str(record.get("l18", "")).strip().casefold()
        l30 = str(record.get("l30", "")).strip().casefold()
        if normalized_symbol and (normalized_symbol in l18 or normalized_symbol in l30):
            return record
    return None


def fetch_brsapi(brs_url, brs_api_key):
    try:
        data = fetch_json(
            brs_url,
            params={"key": brs_api_key},
            headers=HEADERS,
            quota_bucket=LIVE,
        )
        logger.info("[BRS_FETCH_OK] Successfully fetched gold/currency payload from %s", brs_url)
        return data
    except MarketDataFetchError as exc:
        logger.error("[BRS_FETCH_ERROR] Failed to fetch BRS data from %s: %s", brs_url, exc)
        return None
    except Exception as exc:
        logger.error("[BRS_FETCH_UNEXPECTED] Unexpected error fetching BRS data: %s", exc)
        return None


def fetch_tsetmc(tsetmc_url, tsetmc_api_key):
    try:
        data = fetch_json(
            tsetmc_url,
            params={"key": tsetmc_api_key, "type": "1"},
            headers=HEADERS,
            quota_bucket=LIVE,
        )
        logger.info("[TSETMC_FETCH_OK] Successfully fetched stock payload from %s", tsetmc_url)
        return data
    except MarketDataFetchError as exc:
        logger.error("[TSETMC_FETCH_ERROR] Failed to fetch TSETMC data from %s: %s", tsetmc_url, exc)
        return None
    except Exception as exc:
        logger.error("[TSETMC_FETCH_UNEXPECTED] Unexpected error fetching TSETMC data: %s", exc)
        return None


def fetch_tsetmc_symbol(tsetmc_symbol_url, tsetmc_api_key, symbol):
    try:
        data = fetch_json(
            tsetmc_symbol_url,
            params={"key": tsetmc_api_key, "l18": symbol},
            headers=HEADERS,
            quota_bucket=LIVE,
        )
        logger.info("[TSETMC_SYMBOL_FETCH_OK] Fetched symbol %s from %s", symbol, tsetmc_symbol_url)
        return data
    except QuotaExhausted as exc:
        logger.warning("[TSETMC_SYMBOL_QUOTA_EXHAUSTED] Quota exhausted for symbol fetch %s: %s", symbol, exc)
        return None
    except MarketDataFetchError as exc:
        logger.error("[TSETMC_SYMBOL_FETCH_ERROR] Failed symbol fetch for %s: %s", symbol, exc)
        return None
    except Exception as exc:
        logger.error("[TSETMC_SYMBOL_FETCH_UNEXPECTED] Unexpected error fetching symbol %s: %s", symbol, exc)
        return None


# Both provider jobs are I/O-bound HTTP calls (requests, not asyncio elsewhere in
# this sync Django/Celery codebase), so a thread pool is enough to run them
# concurrently. Worst case per job is (retries+1)*timeout + backoff sleeps ~= 63s;
# sequential BRS+TSETMC could approach the 2-minute Celery beat interval, risking
# overlapping task runs. FETCH_TIMEOUT bounds the combined wait per job.
FETCH_TIMEOUT = 90


def _brs_job(brs_url, brs_key):
    return {"brsapi": fetch_brsapi(brs_url, brs_key)}


def _tsetmc_job(tsetmc_url, tsetmc_key, tsetmc_symbol_url):
    result = {"tsetmc": fetch_tsetmc(tsetmc_url, tsetmc_key)}
    kama_record = _find_symbol_record(result["tsetmc"], KAMA_SYMBOL)
    if _extract_price(kama_record) <= 0:
        result["tsetmc_symbol_kama"] = fetch_tsetmc_symbol(tsetmc_symbol_url, tsetmc_key, KAMA_SYMBOL)
    return result


def fetch_all_markets(api_settings):
    """Return all raw market payloads needed by extractor.extract_standard_prices.

    BRS and TSETMC are fetched in parallel (ThreadPoolExecutor) since they are
    independent HTTP calls; a slow/failing provider must not block the other.
    """
    raw_data = {}
    jobs = []

    brs_url = api_settings.get("brs_url")
    brs_key = api_settings.get("brs_api_key")
    tsetmc_url = api_settings.get("tsetmc_url")
    tsetmc_key = api_settings.get("tsetmc_api_key")

    # ponytail: executor is not used as a context manager on purpose — `with`
    # blocks shutdown() on thread completion, which would defeat the combined
    # timeout below if one job hangs. Threads that miss the deadline keep
    # running in the background and are simply discarded (fetch_json already
    # bounds each HTTP call, so they can't run forever).
    executor = ThreadPoolExecutor(max_workers=2)
    if brs_url and brs_key:
        jobs.append(executor.submit(_brs_job, brs_url, brs_key))
    else:
        logger.warning("[FETCH_SKIP] BRS API URL or Key missing in Django settings.")

    if tsetmc_url and tsetmc_key:
        tsetmc_symbol_url = api_settings.get("tsetmc_symbol_url", settings.TSETMC_SYMBOL_URL)
        jobs.append(executor.submit(_tsetmc_job, tsetmc_url, tsetmc_key, tsetmc_symbol_url))
    else:
        logger.warning("[FETCH_SKIP] TSETMC URL or Key missing in Django settings.")

    done, not_done = wait(jobs, timeout=FETCH_TIMEOUT)
    for job in not_done:
        logger.error("[FETCH_ALL_MARKETS_JOB_TIMEOUT] Provider job did not finish within %ss.", FETCH_TIMEOUT)
    for job in done:
        try:
            raw_data.update(job.result())
        except Exception as exc:
            logger.error("[FETCH_ALL_MARKETS_JOB_ERROR] Provider job raised: %s", exc)
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

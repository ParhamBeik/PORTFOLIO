"""Live market fetch layer for the 2-minute price loop (BRS + TSETMC).

Historical/warehouse fetchers live in `marketdata.fetchers`.
"""
import logging
from django.conf import settings

from marketdata.fetchers.base import MarketDataFetchError, fetch_json
from marketdata.quota import LIVE

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
        return fetch_json(
            brs_url,
            params={"key": brs_api_key},
            headers=HEADERS,
            quota_bucket=LIVE,
        )
    except MarketDataFetchError as exc:
        logger.warning("BRS fetch failed: %s", exc)
        return None


def fetch_tsetmc(tsetmc_url, tsetmc_api_key):
    try:
        return fetch_json(
            tsetmc_url,
            params={"key": tsetmc_api_key, "type": "1"},
            headers=HEADERS,
            quota_bucket=LIVE,
        )
    except MarketDataFetchError as exc:
        logger.warning("TSETMC fetch failed: %s", exc)
        return None


def fetch_tsetmc_symbol(tsetmc_symbol_url, tsetmc_api_key, symbol):
    try:
        return fetch_json(
            tsetmc_symbol_url,
            params={"key": tsetmc_api_key, "l18": symbol},
            headers=HEADERS,
            quota_bucket=LIVE,
        )
    except MarketDataFetchError as exc:
        logger.warning("TSETMC symbol fetch failed for %s: %s", symbol, exc)
        return None


def fetch_all_markets(api_settings):
    """Return all raw market payloads needed by extractor.extract_standard_prices."""
    raw_data = {}
    brs_url = api_settings.get("brs_url")
    brs_key = api_settings.get("brs_api_key")
    if brs_url and brs_key:
        raw_data["brsapi"] = fetch_brsapi(brs_url, brs_key)

    tsetmc_url = api_settings.get("tsetmc_url")
    tsetmc_key = api_settings.get("tsetmc_api_key")
    if tsetmc_url and tsetmc_key:
        raw_data["tsetmc"] = fetch_tsetmc(tsetmc_url, tsetmc_key)
        kama_record = _find_symbol_record(raw_data.get("tsetmc"), KAMA_SYMBOL)
        if _extract_price(kama_record) <= 0:
            raw_data["tsetmc_symbol_kama"] = fetch_tsetmc_symbol(
                api_settings.get("tsetmc_symbol_url", settings.TSETMC_SYMBOL_URL),
                tsetmc_key,
                KAMA_SYMBOL,
            )
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

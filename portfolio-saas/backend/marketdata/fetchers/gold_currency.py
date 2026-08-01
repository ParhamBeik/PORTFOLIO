"""Fetcher for Gold, Currency, and Crypto market endpoints (Free & Pro)."""
import logging
from typing import Any, Dict, Optional

from marketdata import endpoints
from marketdata.fetchers.base import fetch_json
from marketdata.jalali import assert_jalali

logger = logging.getLogger(__name__)


def fetch_gold_currency_free(api_key: str) -> Optional[Dict[str, Any]]:
    """Fetch live free gold, fiat currency, and cryptocurrency prices."""
    if not api_key:
        return None
    endpoint = endpoints.get("gold_currency_free")
    return fetch_json(
        endpoint.url, params={"key": api_key}, quota_bucket=endpoint.bucket
    )


def fetch_gold_currency_pro(api_key: str, section: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch live pro market data for gold, currency, and/or cryptocurrency."""
    if not api_key:
        return None
    params = {"key": api_key}
    if section:
        params["section"] = section
    endpoint = endpoints.get("gold_currency_pro")
    return fetch_json(endpoint.url, params=params, quota_bucket=endpoint.bucket)


def fetch_gold_currency_pro_history_24h(api_key: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch 24-hour intraday price history for a gold or currency symbol."""
    if not api_key or not symbol:
        return None
    endpoint = endpoints.get("gold_currency_history")
    return fetch_json(
        endpoint.url,
        params={"key": api_key, "history": "1", "symbol": symbol},
        quota_bucket=endpoint.bucket,
    )


def fetch_gold_currency_pro_history_daily(
    api_key: str,
    symbol: str,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch the full daily price history for a gold or currency symbol.

    Omitting both dates returns the entire series (~4,800 rows back to 1390) in
    one request, which is what the archive worker wants. The date params exist
    for narrow re-checks only, and must be Jalali.
    """
    if not api_key or not symbol:
        return None
    fetch_symbol = "USDT" if symbol == "USDT_IRT" else symbol
    params = {"key": api_key, "history": "2", "symbol": fetch_symbol}
    if date_start:
        params["date_start"] = assert_jalali(date_start, field="date_start")
    if date_end:
        params["date_end"] = assert_jalali(date_end, field="date_end")
    endpoint = endpoints.get("gold_currency_history")
    return fetch_json(endpoint.url, params=params, quota_bucket=endpoint.bucket)

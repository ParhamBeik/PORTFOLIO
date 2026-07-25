"""Fetcher for Gold, Currency, and Crypto market endpoints (Free & Pro)."""
import logging
from typing import Any, Dict, Optional
from marketdata.fetchers.base import fetch_json
from marketdata.quota import ARCHIVE, LIVE

logger = logging.getLogger(__name__)

GOLD_CURRENCY_FREE_URL = "https://Api.BrsApi.ir/Market/Gold_Currency.php"
GOLD_CURRENCY_PRO_URL = "https://Api.BrsApi.ir/Market/Gold_Currency_Pro.php"


def fetch_gold_currency_free(api_key: str) -> Optional[Dict[str, Any]]:
    """Fetch live free gold, fiat currency, and cryptocurrency prices."""
    if not api_key:
        return None
    return fetch_json(GOLD_CURRENCY_FREE_URL, params={"key": api_key}, quota_bucket=LIVE)


def fetch_gold_currency_pro(api_key: str, section: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch live pro market data for gold, currency, and/or cryptocurrency."""
    if not api_key:
        return None
    params = {"key": api_key}
    if section:
        params["section"] = section
    return fetch_json(GOLD_CURRENCY_PRO_URL, params=params, quota_bucket=LIVE)


def fetch_gold_currency_pro_history_24h(api_key: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch 24-hour intraday price history for a gold or currency symbol."""
    if not api_key or not symbol:
        return None
    params = {"key": api_key, "history": "1", "symbol": symbol}
    return fetch_json(GOLD_CURRENCY_PRO_URL, params=params, quota_bucket=ARCHIVE)


def fetch_gold_currency_pro_history_daily(
    api_key: str,
    symbol: str,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch daily date-range price history for a gold or currency symbol."""
    if not api_key or not symbol:
        return None
    fetch_symbol = "USDT" if symbol == "USDT_IRT" else symbol
    params = {"key": api_key, "history": "2", "symbol": fetch_symbol}
    if date_start:
        params["date_start"] = date_start
    if date_end:
        params["date_end"] = date_end
    return fetch_json(GOLD_CURRENCY_PRO_URL, params=params, quota_bucket=ARCHIVE)

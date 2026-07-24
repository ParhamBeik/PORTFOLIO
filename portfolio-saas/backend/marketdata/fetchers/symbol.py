"""Fetcher for comprehensive TSE stock symbol data, metrics, order book, and assembly notices."""
import logging
from typing import Any, Dict, Optional
from marketdata.fetchers.base import fetch_json
from marketdata.quota import ARCHIVE

logger = logging.getLogger(__name__)

SYMBOL_API_URL = "https://Api.BrsApi.ir/Tsetmc/Symbol.php"


def fetch_symbol_data(api_key: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch complete live symbol metrics, fundamental data, order book depth, and notices."""
    if not api_key or not symbol:
        return None
    return fetch_json(SYMBOL_API_URL, params={"key": api_key, "l18": symbol}, quota_bucket=ARCHIVE)

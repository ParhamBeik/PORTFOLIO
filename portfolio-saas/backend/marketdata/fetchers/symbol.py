"""Fetcher for comprehensive TSE stock symbol data, metrics, order book, and assembly notices."""
import logging
from typing import Any, Dict, Optional

from marketdata import endpoints
from marketdata.fetchers.base import fetch_json

logger = logging.getLogger(__name__)


def fetch_symbol_data(api_key: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch complete live symbol metrics, fundamental data, order book depth, and notices."""
    if not api_key or not symbol:
        return None
    endpoint = endpoints.get("symbol")
    return fetch_json(
        endpoint.url,
        params={"key": api_key, "l18": symbol},
        quota_bucket=endpoint.bucket,
    )

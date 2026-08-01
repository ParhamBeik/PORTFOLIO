"""Fetcher for intraday and daily OHLC candlestick time series."""
import logging
from typing import Any, Dict, Optional

from marketdata import endpoints
from marketdata.fetchers.base import fetch_json

logger = logging.getLogger(__name__)


def fetch_candlesticks(
    api_key: str, symbol: str, candle_type: int = 3
) -> Optional[Dict[str, Any]]:
    """Fetch the full daily OHLC candlestick series for a symbol.

    Args:
        api_key: BRS API Key.
        symbol: Stock symbol (l18).
        candle_type: 2 for daily unadjusted, 3 for daily adjusted. Type 0 is
            rejected with HTTP 400 and type 1 returns status=no_data.
    """
    if not api_key or not symbol:
        return None
    endpoint = endpoints.get("stock_candles")
    if candle_type not in endpoint.valid_types:
        raise ValueError(
            f"candle_type must be one of {endpoint.valid_types}, got {candle_type}."
        )
    return fetch_json(
        endpoint.url,
        params={"key": api_key, "type": str(candle_type), "l18": symbol},
        quota_bucket=endpoint.bucket,
    )

"""Fetcher for intraday and daily OHLC candlestick time series."""
import logging
from typing import Any, Dict, Optional
from marketdata.fetchers.base import fetch_json

logger = logging.getLogger(__name__)

CANDLESTICK_API_URL = "https://Api.BrsApi.ir/Tsetmc/Candlestick.php"


def fetch_candlesticks(
    api_key: str, symbol: str, candle_type: int = 6
) -> Optional[Dict[str, Any]]:
    """Fetch intraday or daily OHLC candlestick series for a symbol.

    Args:
        api_key: BRS API Key.
        symbol: Stock symbol (l18).
        candle_type:
            1: 1 min, 2: 5 min, 3: 15 min, 4: 30 min, 5: 60 min,
            6: Daily Adjusted, 7: Daily Unadjusted.
    """
    if not api_key or not symbol:
        return None
    return fetch_json(
        CANDLESTICK_API_URL,
        params={"key": api_key, "type": str(candle_type), "l18": symbol},
    )

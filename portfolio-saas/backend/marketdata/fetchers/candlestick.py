"""Fetcher for intraday and daily OHLC candlestick time series."""
import logging
from typing import Any, Dict, Optional
from marketdata.fetchers.base import fetch_json
from marketdata.quota import ARCHIVE

logger = logging.getLogger(__name__)

CANDLESTICK_API_URL = "https://Api.BrsApi.ir/Tsetmc/Candlestick.php"


def fetch_candlesticks(
    api_key: str, symbol: str, candle_type: int = 3
) -> Optional[Dict[str, Any]]:
    """Fetch intraday or daily OHLC candlestick series for a symbol.

    Args:
        api_key: BRS API Key.
        symbol: Stock symbol (l18).
        candle_type:
            1: Realtime/intraday, 2: Daily unadjusted, 3: Daily adjusted.
    """
    if not api_key or not symbol:
        return None
    return fetch_json(
        CANDLESTICK_API_URL,
        params={"key": api_key, "type": str(candle_type), "l18": symbol},
        quota_bucket=ARCHIVE,
    )

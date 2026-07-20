"""Fetcher for intraday trade tick transaction data."""
import logging
from typing import Any, Dict, List, Optional
from marketdata.fetchers.base import fetch_json

logger = logging.getLogger(__name__)

TRANSACTION_API_URL = "https://Api.BrsApi.ir/Tsetmc/Transaction.php"


def fetch_transactions(
    api_key: str, symbol: str, date: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """Fetch intraday tick-by-tick trade transactions for a symbol on a given date (YYYY-MM-DD)."""
    if not api_key or not symbol:
        return None
    params = {"key": api_key, "l18": symbol}
    if date:
        params["date"] = date
    return fetch_json(TRANSACTION_API_URL, params=params)

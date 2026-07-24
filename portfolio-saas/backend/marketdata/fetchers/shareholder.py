"""Fetcher for major institutional shareholder rosters and ownership changes."""
import logging
from typing import Any, Dict, List, Optional
from marketdata.fetchers.base import fetch_json
from marketdata.quota import ARCHIVE

logger = logging.getLogger(__name__)

SHAREHOLDER_API_URL = "https://Api.BrsApi.ir/Tsetmc/Shareholder.php"


def fetch_shareholders(
    api_key: str, symbol: str, date: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """Fetch major institutional shareholder holdings for a symbol."""
    if not api_key or not symbol:
        return None
    params = {"key": api_key, "l18": symbol}
    if date:
        params["date"] = date
    return fetch_json(SHAREHOLDER_API_URL, params=params, quota_bucket=ARCHIVE)

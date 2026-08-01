"""Fetcher for major institutional shareholder rosters and ownership changes."""
import logging
from typing import Any, Dict, List, Optional

from marketdata import endpoints
from marketdata.fetchers.base import fetch_json
from marketdata.jalali import assert_jalali

logger = logging.getLogger(__name__)


def fetch_shareholders(
    api_key: str, symbol: str, date: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """Fetch major institutional shareholder holdings for a symbol.

    `date`, when given, must be Jalali YYYY-MM-DD; Gregorian is rejected 400.
    """
    if not api_key or not symbol:
        return None
    params = {"key": api_key, "l18": symbol}
    if date:
        assert_jalali(date, field="date")
        params["date"] = date
    endpoint = endpoints.get("shareholder_records")
    return fetch_json(endpoint.url, params=params, quota_bucket=endpoint.bucket)

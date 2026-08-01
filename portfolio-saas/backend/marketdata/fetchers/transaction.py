"""Fetcher for intraday trade tick transaction data."""
import logging
from typing import Any, Dict, List, Optional

from marketdata import endpoints
from marketdata.fetchers.base import fetch_json
from marketdata.jalali import assert_jalali

logger = logging.getLogger(__name__)


def fetch_transactions(
    api_key: str, symbol: str, date: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """Fetch intraday tick-by-tick trade transactions for a symbol on one Jalali date.

    `date` is required: omitting it makes the provider return an empty list with
    HTTP 200, which is indistinguishable from a genuinely quiet day and silently
    marks a backfill state complete with zero rows.
    """
    if not api_key or not symbol:
        return None
    if not date:
        raise ValueError(
            "fetch_transactions requires an explicit Jalali date; "
            "omitting it returns [] with HTTP 200."
        )
    assert_jalali(date, field="date")
    endpoint = endpoints.get("stock_transaction_ticks")
    return fetch_json(
        endpoint.url,
        params={"key": api_key, "l18": symbol, "date": date},
        quota_bucket=endpoint.bucket,
    )

"""Fetcher for daily stock price history and Real/Legal (حقیقی/حقوقی) trade participant volumes."""
import logging
from typing import Any, Dict, List, Optional, Union

from marketdata import endpoints
from marketdata.fetchers.base import fetch_json

logger = logging.getLogger(__name__)


def fetch_daily_history(
    api_key: str, symbol: str, history_type: int = 0
) -> Optional[Union[List[Any], Dict[str, Any]]]:
    """Fetch the complete daily stock history in one request.

    Args:
        api_key: BRS API Key.
        symbol: TSE Symbol (l18 e.g. 'فملی', 'خودرو').
        history_type: 0 for the unadjusted OHLC price series (date, pf/pl/pc,
            pmin/pmax, tvol/tval/tno); 1 for the Real/Legal (حقیقی/حقوقی)
            participant breakdown (date, Buy_CountI/N, Buy_I_Volume, ...).

    The two payloads are disjoint: type=0 carries no participant fields and
    type=1 carries no price fields. Verified against the live endpoint on
    1405-05-04 (type=0: 4617 records, type=1: 3838 records for فملی). Adjusted
    prices are NOT available here -- they come from Candlestick.php type=3.
    """
    if not api_key or not symbol:
        return None
    endpoint = endpoints.get("stock_history")
    return fetch_json(
        endpoint.url,
        params={"key": api_key, "type": str(history_type), "l18": symbol},
        quota_bucket=endpoint.bucket,
    )

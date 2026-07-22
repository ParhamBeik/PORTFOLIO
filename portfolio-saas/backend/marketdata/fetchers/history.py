"""Fetcher for daily stock price history and Real/Legal (حقیقی/حقوقی) trade participant volumes."""
import logging
from typing import Any, Dict, List, Optional, Union
from marketdata.fetchers.base import fetch_json
from marketdata.quota import ARCHIVE

logger = logging.getLogger(__name__)

HISTORY_API_URL = "https://Api.BrsApi.ir/Tsetmc/History.php"


def fetch_daily_history(
    api_key: str, symbol: str, history_type: int = 0
) -> Optional[Union[List[Any], Dict[str, Any]]]:
    """Fetch daily stock history.

    Args:
        api_key: BRS API Key.
        symbol: TSE Symbol (l18 e.g. 'فملی', 'خودرو').
        history_type: 0 for unadjusted price history & Real/Legal distribution, 1 for adjusted price history.
    """
    if not api_key or not symbol:
        return None
    return fetch_json(
        HISTORY_API_URL,
        params={"key": api_key, "type": str(history_type), "l18": symbol},
        quota_bucket=ARCHIVE,
    )

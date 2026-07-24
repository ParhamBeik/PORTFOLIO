"""Fetcher for Tehran Stock Exchange overall and equal-weight market index metrics."""
import logging
from typing import Any, Dict, Optional
from marketdata.fetchers.base import fetch_json
from marketdata.quota import ARCHIVE

logger = logging.getLogger(__name__)

INDEX_API_URL = "https://Api.BrsApi.ir/Tsetmc/Index.php"


def fetch_market_index(api_key: str, index_type: int = 1) -> Optional[Dict[str, Any]]:
    """Fetch market index payload for overall and equal-weight TSE indices."""
    if not api_key:
        return None
    return fetch_json(INDEX_API_URL, params={"key": api_key, "type": str(index_type)}, quota_bucket=ARCHIVE)

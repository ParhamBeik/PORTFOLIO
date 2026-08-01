"""Fetcher for Tehran Stock Exchange overall and equal-weight market index metrics."""
import logging
from typing import Any, Dict, Optional

from marketdata import endpoints, market_state
from marketdata.fetchers.base import fetch_json

logger = logging.getLogger(__name__)


def fetch_market_index(api_key: str, index_type: int = 1) -> Optional[Dict[str, Any]]:
    """Fetch the live index snapshot for overall (type 1) or equal-weight (type 2) TSE indices.

    The response carries a `state` field with the market status, which is the
    only open/closed signal the provider offers. There is no index history here:
    a `date` param is accepted but ignored.
    """
    if not api_key:
        return None
    endpoint = endpoints.get("market_index")
    payload = fetch_json(
        endpoint.url,
        params={"key": api_key, "type": str(index_type)},
        quota_bucket=endpoint.bucket,
    )
    # Free side effect: this response is the only place the provider tells us
    # whether the session is actually open (holidays included).
    market_state.remember_provider_state(payload)
    return payload

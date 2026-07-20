"""Fetcher for Codal financial disclosures, quarterly reports, and corporate filings."""
import logging
from typing import Any, Dict, Optional
from marketdata.fetchers.base import fetch_json

logger = logging.getLogger(__name__)

CODAL_API_URL = "https://Api.BrsApi.ir/Codal/Announcement.php"


def fetch_codal_announcements(
    api_key: str,
    symbol: Optional[str] = None,
    category: Optional[int] = None,
    audited: Optional[bool] = None,
    unaudited: Optional[bool] = None,
    only_main_company: Optional[bool] = None,
    only_subsidiaries: Optional[bool] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    page: int = 1,
) -> Optional[Dict[str, Any]]:
    """Fetch Codal announcements with optional filtering options."""
    if not api_key:
        return None
    params: Dict[str, Any] = {"key": api_key, "page": page}
    if symbol:
        params["l18"] = symbol
    if category is not None:
        params["category"] = category
    if audited is not None:
        params["audited"] = "true" if audited else "false"
    if unaudited is not None:
        params["unaudited"] = "true" if unaudited else "false"
    if only_main_company is not None:
        params["only_main_company"] = "true" if only_main_company else "false"
    if only_subsidiaries is not None:
        params["only_subsidiaries"] = "true" if only_subsidiaries else "false"
    if date_start:
        params["date_start"] = date_start
    if date_end:
        params["date_end"] = date_end
    return fetch_json(CODAL_API_URL, params=params)

"""Base HTTP client for market fetchers with retries and timeout management."""
import logging
import time
from typing import Any, Dict, Optional
import requests

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


def fetch_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    retries: int = 2,
    backoff_factor: float = 1.0,
) -> Optional[Any]:
    """Execute an HTTP GET request to fetch JSON payload with backoff retries."""
    req_headers = {**DEFAULT_HEADERS, **(headers or {})}
    attempt = 0
    while attempt <= retries:
        try:
            response = requests.get(url, params=params, headers=req_headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            attempt += 1
            if attempt > retries:
                logger.warning("HTTP fetch failed for %s (params=%s): %s", url, params, exc)
                return None
            time.sleep(backoff_factor * (2 ** (attempt - 1)))
    return None

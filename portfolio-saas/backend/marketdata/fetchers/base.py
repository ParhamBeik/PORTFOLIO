"""Base HTTP client for market fetchers with retries and timeout management."""
import logging
import time
from typing import Any, Dict, Optional
import requests

from marketdata.quota import OTHER, QuotaExhausted, reserve_request

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


class MarketDataFetchError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class PermanentMarketDataError(MarketDataFetchError):
    pass


class TransientMarketDataError(MarketDataFetchError):
    pass


def _safe_params(params):
    return {
        key: "***" if "key" in str(key).lower() else value
        for key, value in (params or {}).items()
    }


def fetch_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    retries: int = 2,
    backoff_factor: float = 1.0,
    quota_bucket: str = OTHER,
) -> Optional[Any]:
    """Execute an HTTP GET request to fetch JSON payload with backoff retries."""
    req_headers = {**DEFAULT_HEADERS, **(headers or {})}
    attempt = 0
    while attempt <= retries:
        reserve_request(quota_bucket)
        try:
            response = requests.get(url, params=params, headers=req_headers, timeout=timeout)
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise PermanentMarketDataError(
                    f"Provider rejected request with HTTP {response.status_code}.",
                    status_code=response.status_code,
                )
            response.raise_for_status()
            return response.json()
        except (PermanentMarketDataError, QuotaExhausted):
            raise
        except requests.exceptions.RequestException as exc:
            attempt += 1
            if attempt > retries:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                logger.warning(
                    "HTTP fetch failed for %s (params=%s): %s status=%s",
                    url,
                    _safe_params(params),
                    type(exc).__name__,
                    status_code,
                )
                raise TransientMarketDataError(
                    f"Provider request failed ({type(exc).__name__}, status={status_code}).",
                    status_code=status_code,
                ) from exc
            time.sleep(backoff_factor * (2 ** (attempt - 1)))
    raise TransientMarketDataError("Provider request failed.")

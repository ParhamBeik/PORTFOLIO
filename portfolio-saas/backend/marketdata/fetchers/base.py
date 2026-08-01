"""Base HTTP client for market fetchers with retries and timeout management."""
import logging
import time
from typing import Any, Dict, Optional

import requests

from marketdata.quota import OTHER, QuotaExhausted, reconcile_account, reserve_request

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


def _extract_account(response):
    """Pull the provider's `account` block out of any response, success or error.

    The block rides on every envelope (and on rate-limit error bodies), reporting
    `usage_today` and `request_block`. Swallowing parse failures here keeps a
    malformed body from masking the real HTTP error that the caller will raise.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        account = body.get("account")
        if isinstance(account, dict):
            return account
    return None


def fetch_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    retries: int = 2,
    backoff_factor: float = 1.0,
    quota_bucket: str = OTHER,
) -> Optional[Any]:
    """Execute an HTTP GET request to fetch JSON payload with backoff retries.

    Quota is reserved **once per logical fetch**, before the retry loop: a single
    fetch that retries on a transient error must not burn one quota unit per
    attempt. The provider's `account` block (when present on a response) is
    reconciled to `ApiRequestQuota.used` so the local counter self-heals drift
    from worker restarts, dropped responses, and manual probing; `request_block`
    is honored as a real backoff signal on rate-limited responses.
    """
    req_headers = {**DEFAULT_HEADERS, **(headers or {})}
    reserve_request(quota_bucket)
    attempt = 0
    while attempt <= retries:
        try:
            response = requests.get(url, params=params, headers=req_headers, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            attempt += 1
            if attempt > retries:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                logger.warning(
                    "HTTP fetch failed for %s (params=%s): %s status=%s",
                    url, _safe_params(params), type(exc).__name__, status_code,
                )
                raise TransientMarketDataError(
                    f"Provider request failed ({type(exc).__name__}, status={status_code}).",
                    status_code=status_code,
                ) from exc
            time.sleep(backoff_factor * (2 ** (attempt - 1)))
            continue

        # The provider is the source of truth for quota: reconcile our counter to
        # its `usage_today` whenever it exposes one, before deciding to retry.
        block = reconcile_account(_extract_account(response))

        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise PermanentMarketDataError(
                f"Provider rejected request with HTTP {response.status_code}.",
                status_code=response.status_code,
            )

        if response.status_code == 429:
            # `request_block` (seconds) is the provider's own backoff ask; fall back
            # to exponential backoff when it is absent. Cap so a misreported value
            # can't stall the worker indefinitely.
            wait = min(block or backoff_factor * (2 ** attempt), 120)
            attempt += 1
            if attempt > retries:
                raise TransientMarketDataError(
                    "Provider rate-limited (HTTP 429) after retries.",
                    status_code=429,
                )
            logger.info("[QUOTA] HTTP 429; backing off %.1fs.", wait)
            time.sleep(wait)
            continue

        try:
            return response.json()
        except ValueError as exc:
            attempt += 1
            if attempt > retries:
                raise TransientMarketDataError(
                    f"Provider returned non-JSON body (HTTP {response.status_code}).",
                    status_code=response.status_code,
                ) from exc
            time.sleep(backoff_factor * (2 ** (attempt - 1)))
    raise TransientMarketDataError("Provider request failed.")

"""API client fetchers for expanded BrsApi endpoints (Index, ETF NAV, Options, Commodities, Crypto)."""
import logging

from django.conf import settings

from marketdata import endpoints

from .base import fetch_json

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
}


def _fetch(key, params):
    endpoint = endpoints.get(key)
    return fetch_json(
        endpoint.url, params=params, headers=HEADERS, quota_bucket=endpoint.bucket
    )


def fetch_etf_navs():
    """Fetch every ETF's NAV in one request.

    No `l18`: the provider returns the full ETF list, and passing a non-ETF
    symbol returns 502. Fanning this out per symbol is both wasteful and wrong.
    """
    return _fetch("etf_nav", {"key": settings.TSETMC_API_KEY})


def fetch_option_contracts(symbol: str = ""):
    """Fetch Options market contracts and Greeks data."""
    params = {"key": settings.TSETMC_API_KEY}
    if symbol:
        params["l18"] = symbol
    return _fetch("option_contracts", params)


def fetch_commodity_prices(symbol: str = ""):
    """Fetch precious metals, industrial metals, and energy quotes."""
    params = {"key": settings.BRS_API_KEY}
    if symbol:
        params["symbol"] = symbol
    return _fetch("commodity", params)


def fetch_crypto_prices(symbol: str = ""):
    """Fetch live cryptocurrency prices, volumes, and market caps."""
    params = {"key": settings.BRS_API_KEY}
    if symbol:
        params["symbol"] = symbol
    return _fetch("crypto", params)

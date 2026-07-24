"""API client fetchers for expanded BrsApi endpoints (Index, ETF NAV, Options, Commodities, Crypto, Transactions)."""
import logging
from django.conf import settings

from .base import fetch_json
from marketdata.quota import ARCHIVE

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
}


def fetch_index_history(symbol: str = ""):
    """Fetch market index history (TEPIX, Equal-Weighted, etc.) from BrsApi."""
    url = getattr(settings, "BRS_INDEX_URL", "https://Api.BrsApi.ir/Tsetmc/Index.php")
    key = getattr(settings, "TSETMC_API_KEY", "")
    params = {"key": key}
    if symbol:
        params["l18"] = symbol
    return fetch_json(url, params=params, headers=HEADERS, quota_bucket=ARCHIVE)


def fetch_etf_nav_history(symbol: str = ""):
    """Fetch ETF Funds daily NAV and market price metrics from BrsApi."""
    url = getattr(settings, "BRS_ETF_NAV_URL", "https://Api.BrsApi.ir/Tsetmc/EtfNav.php")
    key = getattr(settings, "TSETMC_API_KEY", "")
    params = {"key": key}
    if symbol:
        params["l18"] = symbol
    return fetch_json(url, params=params, headers=HEADERS, quota_bucket=ARCHIVE)


def fetch_option_contracts(symbol: str = ""):
    """Fetch Options market contracts and Greeks data from BrsApi."""
    url = getattr(settings, "BRS_OPTION_URL", "https://Api.BrsApi.ir/Tsetmc/Option.php")
    key = getattr(settings, "TSETMC_API_KEY", "")
    params = {"key": key}
    if symbol:
        params["l18"] = symbol
    return fetch_json(url, params=params, headers=HEADERS, quota_bucket=ARCHIVE)


def fetch_commodity_history(symbol: str = ""):
    """Fetch precious metals, industrial metals, and energy quotes from BrsApi."""
    url = getattr(settings, "BRS_COMMODITY_URL", "https://Api.BrsApi.ir/Market/Commodity.php")
    key = getattr(settings, "BRS_API_KEY", "")
    params = {"key": key}
    if symbol:
        params["symbol"] = symbol
    return fetch_json(url, params=params, headers=HEADERS, quota_bucket=ARCHIVE)


def fetch_crypto_history(symbol: str = ""):
    """Fetch 3,000+ cryptocurrency daily prices, volumes, and market caps from BrsApi."""
    url = getattr(settings, "BRS_CRYPTO_URL", "https://Api.BrsApi.ir/Market/Crypto.php")
    key = getattr(settings, "BRS_API_KEY", "")
    params = {"key": key}
    if symbol:
        params["symbol"] = symbol
    return fetch_json(url, params=params, headers=HEADERS, quota_bucket=ARCHIVE)


def fetch_transaction_ticks(symbol: str, date: str = ""):
    """Fetch granular intraday trade transaction logs for a stock from BrsApi."""
    url = getattr(settings, "BRS_TRANSACTION_URL", "https://Api.BrsApi.ir/Tsetmc/Transaction.php")
    key = getattr(settings, "TSETMC_API_KEY", "")
    params = {"key": key, "l18": symbol}
    if date:
        params["date"] = date
    return fetch_json(url, params=params, headers=HEADERS, quota_bucket=ARCHIVE)

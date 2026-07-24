"""Fetcher for the provider's complete TSETMC instrument catalog."""
from marketdata.fetchers.base import fetch_json
from marketdata.quota import ARCHIVE

ALL_SYMBOLS_API_URL = "https://Api.BrsApi.ir/Tsetmc/AllSymbols.php"


def fetch_all_symbols(api_key: str):
    if not api_key:
        return None
    return fetch_json(
        ALL_SYMBOLS_API_URL,
        params={"key": api_key, "type": "1"},
        quota_bucket=ARCHIVE,
    )

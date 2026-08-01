"""Fetcher for the provider's complete TSETMC instrument catalog."""
from marketdata import endpoints
from marketdata.fetchers.base import fetch_json


def fetch_all_symbols(api_key: str):
    if not api_key:
        return None
    endpoint = endpoints.get("all_symbols")
    return fetch_json(
        endpoint.url,
        params={"key": api_key, "type": "1"},
        quota_bucket=endpoint.bucket,
    )

"""Live derivative-contract snapshots from BRSAPI."""
from .base import fetch_json
from marketdata import endpoints


def fetch_derivatives(api_key, endpoint_key):
    endpoint = endpoints.get(endpoint_key)
    return fetch_json(endpoint.url, params={"key": api_key}, quota_bucket=endpoint.bucket)


def fetch_etf_nav(api_key, symbol):
    """One ETF's NAV. Unlike fetch_derivatives, `etf_nav` requires `l18` per
    request (see marketdata/endpoints.py) -- there is no batch form."""
    endpoint = endpoints.get("etf_nav")
    return fetch_json(
        endpoint.url,
        params={"key": api_key, "l18": symbol},
        quota_bucket=endpoint.bucket,
    )

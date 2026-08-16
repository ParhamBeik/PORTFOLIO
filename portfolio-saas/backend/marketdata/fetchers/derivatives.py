"""Live derivative-contract snapshots from BRSAPI."""
from .base import fetch_json
from marketdata import endpoints


def fetch_derivatives(api_key, endpoint_key):
    endpoint = endpoints.get(endpoint_key)
    return fetch_json(endpoint.url, params={"key": api_key}, quota_bucket=endpoint.bucket)

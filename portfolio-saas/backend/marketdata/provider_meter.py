"""Read the same product counters displayed in BrsApi's account panel.

Unlike an intentionally malformed market-data request, this is a read-only
account endpoint. It is not enabled until its VPS response and billing behavior
have been checked with the account owner.
"""
import re

import requests
from django.conf import settings

from .quota import AIO, MARKET_CGCC


PANEL_URL = "https://api.brsapi.ir/Panel/z_user_overview.php"
_USAGE = re.compile(r"^\s*([\d,]+)\s*/\s*([\d,]+)\s*$")


class ProviderMeterUnavailable(RuntimeError):
    pass


def parse_panel_metrics(payload):
    """Require both product rows before changing any local quota record."""
    if not isinstance(payload, dict) or payload.get("successful") is not True:
        raise ProviderMeterUnavailable("Provider panel did not confirm success.")
    metrics = payload.get("metrics")
    if not isinstance(metrics, list):
        raise ProviderMeterUnavailable("Provider panel did not return metrics.")
    parsed = {}
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        name = str(metric.get("Name_En") or "").upper()
        plan = AIO if name.startswith("AIO") else MARKET_CGCC if name == "MARKET_CGCC" else None
        if plan is None:
            continue
        match = _USAGE.fullmatch(str(metric.get("Usage_To_Limit") or ""))
        if not match or plan in parsed:
            raise ProviderMeterUnavailable(f"Invalid or duplicate {plan} meter.")
        used, limit = (int(value.replace(",", "")) for value in match.groups())
        parsed[plan] = {"used": used, "limit": limit}
    if set(parsed) != {AIO, MARKET_CGCC}:
        raise ProviderMeterUnavailable("Provider panel omitted a required product meter.")
    return parsed


def read_panel_metrics():
    phone = settings.BRSAPI_ACCOUNT_PHONE
    key = settings.TSETMC_API_KEY
    if not phone or not key:
        raise ProviderMeterUnavailable("Panel credentials are not configured.")
    try:
        response = requests.get(
            PANEL_URL,
            params={"Phone": phone, "Key": key},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if response.status_code != 200:
            raise ProviderMeterUnavailable(f"Provider panel returned HTTP {response.status_code}.")
        return parse_panel_metrics(response.json())
    except (requests.RequestException, ValueError):
        # requests exceptions include the URL (and its query credentials) in
        # their text. Never chain or log one from this account endpoint.
        raise ProviderMeterUnavailable("Provider panel could not be read.") from None

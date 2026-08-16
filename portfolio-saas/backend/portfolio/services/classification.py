"""Canonical asset-class resolution for catalog, optimizer, and UI."""
from __future__ import annotations

from portfolio.models import Asset

POLICY_VERSION = "balanced-v1"

# Gold and Cash/FX are one economic sleeve in Iran (IRR hedge), even when
# daily return correlation sits below the statistical cluster threshold.
HARD_ASSET_SLEEVE = {
    "id": "hard_asset",
    "label": "Gold + Cash/FX",
    "classes": ("Gold", "Cash"),
    "max_weight": 0.50,
}

BALANCED_CONSTRAINTS = {
    "long_only": True,
    "max_weight_per_asset": 0.25,
    "max_weight_per_correlation_group": 0.40,
    "max_weight_per_class": {
        "Stock": 0.60,
        "Gold": 0.40,
        "Cash": 0.40,
        "Crypto": 0.15,
        "Real Estate": 0.20,
        "Other": 0.20,
    },
    "correlation_threshold": 0.80,
    "policy_version": POLICY_VERSION,
}

_CRYPTO_KEY_HINTS = ("bitcoin", "btc", "crypto", "eth")
_CASH_KEY_HINTS = ("usd", "usdt", "eur", "cash")


def normalize_asset_class(value: str | None) -> str:
    if not value:
        return "Other"
    if value in BALANCED_CONSTRAINTS["max_weight_per_class"]:
        return value
    if value == Asset.AssetClass.REAL_ESTATE:
        return "Real Estate"
    return "Other"


def classify_from_market_instrument(mi) -> str:
    from marketdata.models import MarketInstrument

    group = (mi.provider_group or "").lower()
    symbol = (mi.symbol or "").lower()
    if mi.category == MarketInstrument.Category.STOCK:
        return "Stock"
    if mi.category == MarketInstrument.Category.GOLD:
        return "Gold"
    if "crypto" in group or "bitcoin" in symbol or "btc" in symbol:
        return "Crypto"
    if "currency" in group or "cash" in group or symbol in ("usd", "usdt", "eur"):
        return "Cash"
    return "Other"


def classify_from_key(key: str) -> str:
    lower = key.lower()
    if any(h in lower for h in _CRYPTO_KEY_HINTS):
        return "Crypto"
    if any(h in lower for h in _CASH_KEY_HINTS):
        return "Cash"
    if "gold" in lower or "coin" in lower or "emami" in lower:
        return "Gold"
    if "house" in lower or "estate" in lower:
        return "Real Estate"
    if "stock" in lower:
        return "Stock"
    return "Other"


def asset_class_map(universe: list[str] | None = None) -> dict[str, str]:
    from portfolio.services.returns import resolve_universe
    from marketdata.models import MarketInstrument

    resolved = resolve_universe(universe)
    cls_map: dict[str, str] = {}
    for item in resolved:
        key = item["key"]
        asset = item.get("asset")
        if asset is not None:
            cls_map[key] = normalize_asset_class(asset.asset_class)
            continue
        symbol = item.get("symbol") or ""
        mi = MarketInstrument.objects.filter(symbol=symbol).first()
        if mi is not None:
            cls_map[key] = classify_from_market_instrument(mi)
        else:
            cls_map[key] = classify_from_key(key)
    return cls_map


def class_totals(weights: dict[str, float], class_map: dict[str, str]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for key, w in weights.items():
        cls = class_map.get(key, "Other")
        totals[cls] = totals.get(cls, 0.0) + float(w)
    return {k: round(v, 6) for k, v in totals.items() if v > 1e-9}

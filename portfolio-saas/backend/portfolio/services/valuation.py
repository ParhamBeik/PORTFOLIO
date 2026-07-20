"""Valuation engine: holdings x latest prices -> portfolio value.

This is the SaaS port of PORTFOLIO NEW STRUCTURE/src/engine.py `build_snapshot`.
Two scale levers live here:
  1. latest prices are read once (DISTINCT ON) and cached, not per-asset;
  2. valuation is pure arithmetic over a preloaded holding set.
"""
from decimal import Decimal

from django.core.cache import cache

from ..models import Account, Asset, Holding, Price

_LATEST_PRICES_CACHE_KEY = "prices:latest"
_HOUSE_AREA_SQM = Decimal("90.2")
_HOUSE_MORTGAGE_DEDUCTION = Decimal("400000000")


def _q(value) -> Decimal:
    """Coerce to Decimal, treating anything non-numeric as zero."""
    try:
        return Decimal(value)
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")


def get_latest_prices() -> dict:
    """Return {asset_key: Decimal price in Tomans}, cached ~10s.

    Uses Postgres DISTINCT ON to fetch the newest price for every asset in a
    single query, so this is O(1) regardless of how many assets or users exist.
    """
    cached = cache.get(_LATEST_PRICES_CACHE_KEY)
    if cached is not None:
        return cached

    latest = (
        Price.objects.select_related("asset")
        .filter(asset__is_active=True)
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    )
    prices = {row.asset.key: _q(row.price) for row in latest}
    # TTL matches the fetch cadence so the cache only misses when the fetcher
    # explicitly invalidates it, not on a timer (M1: no periodic stampede).
    cache.set(_LATEST_PRICES_CACHE_KEY, prices, timeout=120)
    return prices


def invalidate_prices_cache() -> None:
    """Called after a fresh fetch so reads immediately see new prices."""
    cache.delete(_LATEST_PRICES_CACHE_KEY)


def _house_value(price_per_sqm_million: Decimal) -> Decimal:
    """Port of engine.calculate_house_value: area * price/sqm - mortgage."""
    sqm_price = _q(price_per_sqm_million) * Decimal("1000000")
    return sqm_price * _HOUSE_AREA_SQM - _HOUSE_MORTGAGE_DEDUCTION


def asset_value(holding: Holding, price: Decimal) -> Decimal:
    """Quantity x unit price, or the house formula for real estate."""
    if holding.asset.is_house:
        return _house_value(holding.quantity)
    return _q(holding.quantity) * _q(price)


def value_account(account: Account, prices: dict | None = None) -> dict:
    """Compute one account's per-asset values and total.

    Returns: {'total': Decimal, 'items': [{'asset','key','class','quantity',
    'unit_price','value'}]}
    """
    prices = prices if prices is not None else get_latest_prices()
    items, total = [], Decimal("0")
    holdings = (
        account.holdings.select_related("asset")
        if account.pk
        else Holding.objects.none()
    )
    for holding in holdings:
        # None when the asset has no price yet — distinguishable from a real 0 (M2).
        unit_price = prices.get(holding.asset.key)
        value = asset_value(holding, unit_price)
        total += value
        items.append({
            "asset": holding.asset.name,
            "key": holding.asset.key,
            "class": holding.asset.asset_class,
            "quantity": holding.quantity,
            "unit_price": unit_price,
            "value": value,
        })
    return {"total": total, "items": items}


def value_user(user) -> dict:
    """Aggregate valuation across all of a user's accounts."""
    prices = get_latest_prices()
    accounts, total = [], Decimal("0")
    for account in user.accounts.all():
        valuation = value_account(account, prices)
        total += valuation["total"]
        accounts.append({
            "id": account.id,
            "name": account.name,
            "broker": account.broker,
            "total": valuation["total"],
            "items": valuation["items"],
        })
    return {"total": total, "accounts": accounts, "prices": prices}

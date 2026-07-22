"""Cache + DISTINCT ON behaviour of get_latest_prices.

These are the scale levers: one query for the newest price per asset, cached so
reads stay cheap. The DISTINCT ON clause is Postgres-only, which is why these
tests require a real postgres (not sqlite).
"""
from decimal import Decimal

import pytest
from django.core.cache import cache

from marketdata.models import GoldCurrencyHistory
from portfolio.models import Price
from portfolio.services import get_latest_prices, invalidate_prices_cache

pytestmark = pytest.mark.django_db


def test_latest_price_is_newest_per_asset(asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("400000000")})
    write_prices({"emami_coin": Decimal("480000000")})  # newer
    cache.delete("prices:latest")

    prices = get_latest_prices()
    assert prices["emami_coin"] == Decimal("480000000")


def test_latest_prices_is_cached(asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("480000000")})
    first = get_latest_prices()

    # Add a newer row WITHOUT busting the cache; the cached read must not see it.
    Price.objects.create(asset=asset_catalog["emami_coin"], price=Decimal("1"), source="TEST")
    second = get_latest_prices()
    assert second == first
    assert second["emami_coin"] == Decimal("480000000")


def test_invalidate_forces_refresh(asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("480000000")})
    get_latest_prices()  # populate cache
    write_prices({"emami_coin": Decimal("500000000")})

    invalidate_prices_cache()
    refreshed = get_latest_prices()
    assert refreshed["emami_coin"] == Decimal("500000000")


def test_inactive_assets_are_excluded(asset_catalog, write_prices):
    from portfolio.models import Asset

    write_prices({"emami_coin": Decimal("480000000")})
    Asset.objects.filter(key="emami_coin").update(is_active=False)
    cache.delete("prices:latest")
    prices = get_latest_prices()
    assert "emami_coin" not in prices


def test_latest_price_uses_archive_when_latest_fetch_sharply_drops(asset_catalog):
    gold = asset_catalog["emami_coin"]
    gold.brs_symbol = "IR_COIN_EMAMI"
    gold.save(update_fields=["brs_symbol"])
    Price.objects.create(asset=gold, price=Decimal("480000000"), source="SEED")
    Price.objects.create(asset=gold, price=Decimal("1"), source="BAD_FETCH")
    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_EMAMI",
        date="1404-01-02",
        close_price=Decimal("479000000"),
    )
    cache.delete("prices:latest")

    prices = get_latest_prices()
    assert prices["emami_coin"] == Decimal("479000000")

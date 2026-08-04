"""Shared pytest fixtures.

Puts vendored legacy oracle modules on sys.path so portfolio.live.extractor can
be parity-checked against the frozen engine copy under tests/legacy_oracle/.
The DB fixtures mirror seed_assets so tests do not depend on a seeded database.
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent  # .../portfolio-saas/backend
LEGACY_SRC = Path(__file__).resolve().parent / "legacy_oracle"
if str(LEGACY_SRC) not in sys.path:
    sys.path.insert(0, str(LEGACY_SRC))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_market_sample():
    """A synthetic-but-realistic raw market payload exercising every pricing rule."""
    with open(FIXTURES_DIR / "raw_market_sample.json") as fh:
        return json.load(fh)


@pytest.fixture
def legacy_engine():
    """The original src/engine.py — used as the extraction parity oracle."""
    import engine  # noqa: F401  (imported for the side effect of resolving it)
    return engine


@pytest.fixture
def asset_catalog(db):
    """Broad test catalog covering production and legacy/manual asset paths."""
    from portfolio.models import Asset

    entries = [
        ("emami_coin", "Gold", "IRT", False, False),
        ("half_coin", "Gold", "IRT", False, False),
        ("quarter_coin", "Gold", "IRT", False, False),
        ("quarter_coin_pre86", "Gold", "IRT", False, False),
        ("one_gram_coin", "Gold", "IRT", False, False),
        ("swiss_gold_bar_1g", "Gold", "IRT", True, False),
        ("swiss_gold_bar_2_5g", "Gold", "IRT", True, False),
        ("gold_18k_gram", "Gold", "IRT", False, False),
        ("usd_cash", "Cash", "USD", False, False),
        ("usdt_irt", "Cash", "IRT", False, False),
        ("euro_cash", "Cash", "IRT", False, False),
        ("kama_stock", "Stock", "IRT", False, False),
        ("bitcoin_usd", "Crypto", "USD", False, False),
        ("house_asset", "Real Estate", "IRT", False, True),
    ]
    for key, cls, cur, manual, house in entries:
        Asset.objects.get_or_create(
            key=key,
            defaults={
                "name": key.replace("_", " ").title(),
                "asset_class": cls,
                "currency": cur,
                "is_manual": manual,
                "is_house": house,
            },
        )
    return {a.key: a for a in Asset.objects.all()}


@pytest.fixture
def write_prices(db):
    """Return a helper that writes Price rows and busts the latest-prices cache."""
    from django.core.cache import cache
    from portfolio.models import Asset, Price

    def _write(prices: dict) -> dict:
        for key, value in prices.items():
            asset, _ = Asset.objects.get_or_create(
                key=key,
                defaults={"name": key, "asset_class": "Gold", "currency": "IRT"},
            )
            Price.objects.create(asset=asset, price=value, source="TEST")
        cache.delete("prices:latest")
        return prices

    return _write


@pytest.fixture
def make_user(db):
    """Return a helper that creates a user with an explicit tier."""
    from accounts.models import User

    def _make(email="user@test.test", tier=User.Tier.FREE, password="Sup3rSecret!", first_name="", last_name=""):
        user = User.objects.create_user(email=email, password=password, first_name=first_name, last_name=last_name)
        user.tier = tier
        user.save(update_fields=["tier"])
        return user

    return _make


@pytest.fixture(autouse=True)
def clear_caches():
    """Bust all caches before each test.

    LocMemCache persists across the entire pytest process. Views like
    MarketAssetsView cache their full response (``market:assets:catalog:v2``),
    so a test that creates new assets and then hits the endpoint sees stale
    data from a prior test.  ``cache.clear()`` prevents this.

    Also clears pricing locks so concurrent test suites don't block each other.
    """
    from django.core.cache import cache
    cache.clear()
    try:
        from portfolio.live.pubsub import get_redis
        r = get_redis()
        r.delete("lock:price_fetch")
    except Exception:
        pass

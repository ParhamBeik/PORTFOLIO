"""fetch_prices management command.

This is the single entry point that keeps prices fresh and snapshots every user.
The tests mock the network fetch so they run offline and assert the write
behaviour (C2 fix: network stays outside the transaction).
"""
from decimal import Decimal
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command

from accounts.models import User
from portfolio.models import Account, Price, Snapshot

pytestmark = pytest.mark.django_db


def _patch_fetch(monkeypatch, payload):
    import portfolio.tasks as mod

    monkeypatch.setattr(mod, "fetch_all_markets", lambda _settings: payload)


def test_fetch_writes_prices_and_snapshots(asset_catalog, raw_market_sample, monkeypatch):
    user = User.objects.create_user(email="fetch@test.test", password="Sup3rSecret!")
    Account.objects.create(user=user, name="Main")
    _patch_fetch(monkeypatch, raw_market_sample)

    out = StringIO()
    call_command("fetch_prices", stdout=out)
    output = out.getvalue()
    assert "Price fetch complete" in output

    keys = set(Price.objects.values_list("asset__key", flat=True))
    assert {"emami_coin", "kama_stock", "usd_cash"}.issubset(keys)
    assert Snapshot.objects.filter(user=user).count() == 1
    snap = Snapshot.objects.get(user=user)
    # M4: snapshots store only the derived total now (the global price map lives
    # once in the Price table). This account has no holdings, so its total is 0.
    assert snap.total_value_tomans == 0


def test_fetch_dry_run_writes_nothing(asset_catalog, raw_market_sample, monkeypatch):
    _patch_fetch(monkeypatch, raw_market_sample)

    out = StringIO()
    call_command("fetch_prices", "--dry-run", stdout=out)
    assert Price.objects.count() == 0
    assert Snapshot.objects.count() == 0


def test_fetch_kama_falls_back_to_last_price(asset_catalog, monkeypatch):
    """TSETMC omitting KAMA -> the command reuses the last stored KAMA price."""
    cache.delete("prices:latest")
    Price.objects.create(asset=asset_catalog["kama_stock"], price=Decimal("7777"), source="SEED")

    _patch_fetch(monkeypatch, {"brsapi": {"items": [{"symbol": "USD", "price": 63200}]}, "tsetmc": []})

    out = StringIO()
    call_command("fetch_prices", stdout=out)
    latest = Price.objects.filter(asset__key="kama_stock").order_by("-id").first()
    assert latest is not None and latest.price == Decimal("7777")


def test_fetch_no_users_still_writes_prices(asset_catalog, raw_market_sample, monkeypatch):
    """Prices are global; a fetch with zero users still records the market."""
    _patch_fetch(monkeypatch, raw_market_sample)

    out = StringIO()
    call_command("fetch_prices", stdout=out)
    assert Price.objects.filter(asset__key="emami_coin").exists()
    assert Snapshot.objects.count() == 0

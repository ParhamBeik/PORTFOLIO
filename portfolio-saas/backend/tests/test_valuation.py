"""Valuation engine: holdings x latest prices -> portfolio value.

Covers the two non-trivial pieces of portfolio.services: the real-estate house
formula and the live aggregation across accounts.
"""
from decimal import Decimal

import pytest

from portfolio.models import Account, Holding
from portfolio.services import asset_value, value_account, value_user
from portfolio.services.valuation import _house_value

pytestmark = pytest.mark.django_db


def test_house_formula_is_gross_of_mortgage():
    """Since 0017 the mortgage is a Liability, netted off the account total.

    Subtracting it here too would double-count it: 50M Toman/sqm * 90.2 sqm.
    """
    assert _house_value(Decimal("50")) == Decimal("4510000000")


def test_house_formula_zero_price_is_zero():
    assert _house_value(Decimal("0")) == Decimal("0")


def test_asset_value_uses_house_formula_for_real_estate(asset_catalog):
    house = asset_catalog["house_asset"]
    holding = Holding(asset=house, quantity=Decimal("50"), mortgage_deduction_tomans=Decimal("400000000"))
    # The legacy holding column is deliberately ignored — Liability owns the debt.
    assert asset_value(holding, Decimal("0")) == _house_value(Decimal("50"))


def test_asset_value_multiplies_quantity_for_normal_asset(asset_catalog):
    emami = asset_catalog["emami_coin"]
    holding = Holding(asset=emami, quantity=Decimal("2"))
    assert asset_value(holding, Decimal("480000000")) == Decimal("960000000")


def test_value_account_multiplies_quantity_by_price(asset_catalog, write_prices, make_user):
    write_prices({"emami_coin": Decimal("480000000"), "usd_cash": Decimal("63200")})
    user = make_user()
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2"))

    result = value_account(account)
    assert result["total"] == Decimal("960000000")
    item = next(i for i in result["items"] if i["key"] == "emami_coin")
    assert item["unit_price"] == Decimal("480000000")
    assert item["value"] == Decimal("960000000")
    assert item["source"] == "TEST"
    assert item["priced_at"] is not None
    assert item["age_seconds"] >= 0
    assert item["quality_status"] == "live"
    assert result["priced_assets"] == result["total_assets"] == 1


def test_value_account_marks_missing_quote_unavailable(asset_catalog, make_user):
    user = make_user(email="missing-price@test.test")
    account = Account.objects.create(user=user, name="Missing")
    Holding.objects.create(
        account=account,
        asset=asset_catalog["bitcoin_usd"],
        quantity=Decimal("2"),
    )

    result = value_account(account, prices={})

    assert result["total"] == 0
    assert result["priced_assets"] == 0
    assert result["total_assets"] == 1
    assert result["quality_status"] == "unavailable"
    assert result["items"][0]["value"] is None
    assert result["items"][0]["quality_status"] == "unavailable"
    assert result["excluded"] == [
        {"asset_key": "bitcoin_usd", "reason": "missing_price"}
    ]


def test_value_account_applies_house_formula(asset_catalog, write_prices, make_user):
    write_prices({})
    user = make_user(email="house@test.test")
    account = Account.objects.create(user=user, name="Property")
    Holding.objects.create(
        account=account,
        asset=asset_catalog["house_asset"],
        quantity=Decimal("50"),
        mortgage_deduction_tomans=Decimal("400000000")
    )

    result = value_account(account)
    assert result["total"] == _house_value(Decimal("50"))


def test_value_user_aggregates_across_accounts(asset_catalog, write_prices, make_user):
    write_prices({"emami_coin": Decimal("480000000"), "kama_stock": Decimal("5230")})
    user = make_user(email="agg@test.test")
    brokerage = Account.objects.create(user=user, name="Brokerage")
    cash = Account.objects.create(user=user, name="Cash")
    Holding.objects.create(account=brokerage, asset=asset_catalog["emami_coin"], quantity=Decimal("1"))
    Holding.objects.create(account=cash, asset=asset_catalog["kama_stock"], quantity=Decimal("100"))

    valuation = value_user(user)
    assert valuation["total"] == Decimal("480000000") + Decimal("5230") * Decimal("100")
    assert {a["name"] for a in valuation["accounts"]} == {"Brokerage", "Cash"}


def test_compute_dynamic_net_worth_series(asset_catalog, write_prices, make_user):
    from portfolio.services.valuation import compute_dynamic_net_worth_series
    write_prices({"emami_coin": Decimal("480000000"), "usd_cash": Decimal("60000")})
    user = make_user(email="dynamic@test.test")
    account = Account.objects.create(user=user, name="Dynamic")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("1"))

    series = compute_dynamic_net_worth_series(user, account, days=7)
    assert len(series) == 7
    assert "total" in series[0]
    assert "total_usd" in series[0]
    assert float(series[0]["total"]) > 0
    assert series[0]["is_estimated"] is True
    # Opening-only: constant qty → flat when only latest prices exist.
    assert series[0]["total"] == series[-1]["total"]


def test_compute_dynamic_caps_at_90_days(asset_catalog, write_prices, make_user):
    from portfolio.services.valuation import SYNTHETIC_HISTORY_MAX_DAYS, compute_dynamic_net_worth_series
    write_prices({"emami_coin": Decimal("480000000"), "usd_cash": Decimal("60000")})
    user = make_user(email="dynamic-cap@test.test")
    account = Account.objects.create(user=user, name="Dynamic")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("1"))
    series = compute_dynamic_net_worth_series(user, account, days=365)
    assert len(series) == SYNTHETIC_HISTORY_MAX_DAYS


def test_compute_dynamic_respects_buy_sell_timeline(asset_catalog, write_prices, make_user):
    """With a recent BUY, pre-buy days should not include that position."""
    from django.utils import timezone
    from portfolio.models import LedgerEntry
    from portfolio.services.valuation import compute_dynamic_net_worth_series

    write_prices({"emami_coin": Decimal("100"), "usd_cash": Decimal("60000")})
    user = make_user(email="dynamic-buy@test.test")
    account = Account.objects.create(user=user, name="Traded")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("5"))
    # BUY "now" — walking holdings_as_of zeros qty before this timestamp.
    LedgerEntry.objects.create(
        account=account,
        asset=asset_catalog["emami_coin"],
        kind=LedgerEntry.Kind.BUY,
        quantity=Decimal("5"),
        price_tomans=Decimal("100"),
        amount_tomans=Decimal("500"),
        timestamp=timezone.now(),
        source="manual",
    )

    series = compute_dynamic_net_worth_series(user, account, days=5)
    assert len(series) == 5
    assert float(series[0]["total"]) == 0.0
    assert float(series[-1]["total"]) > 0


def test_guard_price_map_accepts_legitimate_large_moves(asset_catalog, write_prices):
    from portfolio.services.valuation import guard_price_map
    write_prices({"emami_coin": Decimal("500000000")})
    guarded = guard_price_map({"emami_coin": Decimal("560000000")})
    assert guarded["emami_coin"] == Decimal("560000000")


def test_guard_price_map_forward_fills_missing_or_zero_prices(asset_catalog, write_prices):
    from portfolio.services.valuation import guard_price_map
    write_prices({"emami_coin": Decimal("500000000")})
    # Live price fails / returns 0
    guarded = guard_price_map({"emami_coin": Decimal("0")})
    assert guarded["emami_coin"] == Decimal("500000000")  # Forward-fills previous price (flat-line)


def test_guard_price_map_does_not_forward_fill_past_max_sessions(asset_catalog, write_prices):
    """A price older than MAX_FORWARD_FILL_SESSIONS days must not be carried
    forward forever -- it is never re-persisted (see _persistable_prices), so
    its `fetched_at` never advances on its own.
    """
    from datetime import timedelta

    from django.utils import timezone

    from portfolio.models import Price
    from portfolio.services.valuation import MAX_FORWARD_FILL_SESSIONS, guard_price_map

    write_prices({"emami_coin": Decimal("500000000")})
    stale_at = timezone.now() - timedelta(days=MAX_FORWARD_FILL_SESSIONS + 1)
    Price.objects.filter(asset__key="emami_coin").update(fetched_at=stale_at)

    guarded = guard_price_map({"emami_coin": Decimal("0")})
    assert guarded["emami_coin"] == Decimal("0")


def test_quality_status_not_stale_when_tse_closed(asset_catalog, write_prices, make_user, monkeypatch):
    """A TSE stock's last price must not be flagged 'stale' just because the
    session closed hours ago -- the tsetmc live job only runs while state ==
    OPEN, so an old price during CLOSED_DAYTIME/OVERNIGHT is still correct.
    """
    from datetime import timedelta

    from django.utils import timezone

    from portfolio.models import Price

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    write_prices({"kama_stock": Decimal("5230")})
    old_at = timezone.now() - timedelta(hours=8)
    Price.objects.filter(asset__key="kama_stock").update(fetched_at=old_at)

    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")
    user = make_user(email="tse-closed@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(account=account, asset=kama, quantity=Decimal("1"))

    result = value_account(account)
    item = next(i for i in result["items"] if i["key"] == "kama_stock")
    assert item["quality_status"] == "live"


def test_quality_status_stale_when_tse_open_and_price_did_not_refresh(
    asset_catalog, write_prices, make_user, monkeypatch
):
    """The same old price IS a real problem while the market is open -- the
    live job should have refreshed it, so this must still show 'stale'.
    """
    from datetime import timedelta

    from django.utils import timezone

    from portfolio.models import Price

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    write_prices({"kama_stock": Decimal("5230")})
    old_at = timezone.now() - timedelta(minutes=20)
    Price.objects.filter(asset__key="kama_stock").update(fetched_at=old_at)

    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "open")
    user = make_user(email="tse-open@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(account=account, asset=kama, quantity=Decimal("1"))

    result = value_account(account)
    item = next(i for i in result["items"] if i["key"] == "kama_stock")
    assert item["quality_status"] == "stale"

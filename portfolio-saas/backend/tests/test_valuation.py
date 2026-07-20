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


def test_house_formula_uses_area_and_mortgage():
    # 50M toman/sqm * 90.2 sqm - 400M mortgage = 4,110,000,000.
    assert _house_value(Decimal("50")) == Decimal("4110000000")


def test_house_formula_zero_price_is_negative_deduction():
    assert _house_value(Decimal("0")) == Decimal("-400000000")


def test_asset_value_uses_house_formula_for_real_estate(asset_catalog):
    house = asset_catalog["house_asset"]
    holding = Holding(asset=house, quantity=Decimal("50"))
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


def test_value_account_applies_house_formula(asset_catalog, write_prices, make_user):
    write_prices({"usd_cash": Decimal("63200")})
    user = make_user(email="house@test.test")
    account = Account.objects.create(user=user, name="Property")
    Holding.objects.create(account=account, asset=asset_catalog["house_asset"], quantity=Decimal("50"))

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

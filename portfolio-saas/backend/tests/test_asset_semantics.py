from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from portfolio.models import Account, Asset, DailyPriceAverage, Holding, LedgerEntry, Price
from portfolio.serializers import AssetSerializer, LedgerEntrySerializer
from portfolio.admin import LedgerEntryAdmin
from types import SimpleNamespace


def test_usd_and_tether_are_distinct_assets_with_shared_exposure():
    cash = Asset(key="usd_cash", asset_class=Asset.AssetClass.CASH)
    tether = Asset(key="usdt_irt", asset_class=Asset.AssetClass.CASH)

    assert cash.key != tether.key
    assert cash.exposure_group == tether.exposure_group == "usd"
    assert cash.quantity_scale == 1
    assert tether.quantity_scale == 1_000_000
    assert cash.valuation_unit == tether.valuation_unit == "toman"


def test_stock_quote_is_rial_but_portfolio_valuation_is_toman():
    stock = Asset(key="kama_stock", asset_class=Asset.AssetClass.STOCK, tse_symbol="کاما")
    payload = AssetSerializer(stock).data

    assert payload["quote_unit"] == "rial"
    assert payload["valuation_unit"] == "toman"
    assert payload["quantity_scale"] == 1
    assert "currency" not in payload


def test_portfolio_crypto_quote_is_toman_after_provider_conversion():
    bitcoin = Asset(key="bitcoin_usd", asset_class=Asset.AssetClass.CRYPTO)
    assert bitcoin.quote_unit == "toman"
    assert bitcoin.valuation_unit == "toman"


@pytest.mark.django_db
def test_atomic_storage_preserves_fractional_gold_and_rejects_fractional_coin(make_user):
    account = Account.objects.create(user=make_user(), name="Atomic")
    gram = Asset.objects.create(key="gold_18k_gram", name="Gram", asset_class=Asset.AssetClass.GOLD)
    coin = Asset.objects.create(key="emami_coin", name="Coin", asset_class=Asset.AssetClass.GOLD)

    holding = Holding.objects.create(account=account, asset=gram, quantity=Decimal("0.000001"))
    assert holding.quantity_atomic == 1
    assert holding.price_per_sqm_tomans is None
    holding.refresh_from_db()
    assert holding.quantity == Decimal("0.000001")
    assert "quantity" not in {field.name for field in Holding._meta.fields}
    assert "quantity" not in {field.name for field in LedgerEntry._meta.fields}
    with pytest.raises(ValidationError, match="atomic unit"):
        Holding.objects.create(account=account, asset=coin, quantity=Decimal("1.5"))


@pytest.mark.django_db
def test_house_price_is_separate_from_atomic_quantity(make_user):
    user = make_user()
    account = Account.objects.create(user=user, name="Property")
    house = Asset.objects.create(
        key="house-test", name="House", asset_class=Asset.AssetClass.REAL_ESTATE,
        is_house=True, is_manual=True, owner=user,
    )
    holding = Holding.objects.create(account=account, asset=house, quantity=Decimal("24.5"))
    mark = LedgerEntry.objects.create(
        account=account, asset=house, kind=LedgerEntry.Kind.VALUATION_MARK,
        quantity=Decimal("24.5"),
    )

    assert holding.price_per_sqm_tomans == mark.price_per_sqm_tomans == Decimal("24500000")
    assert holding.quantity_atomic is None
    assert mark.quantity_atomic is None
    holding.refresh_from_db()
    mark.refresh_from_db()
    assert holding.quantity == mark.quantity == Decimal("24.5")


@pytest.mark.django_db
def test_holding_asset_change_recalculates_atomic_scale(make_user):
    account = Account.objects.create(user=make_user(), name="Atomic edit")
    gram = Asset.objects.create(key="gold_18k_gram", name="Gram", asset_class=Asset.AssetClass.GOLD)
    coin = Asset.objects.create(key="emami_coin", name="Coin", asset_class=Asset.AssetClass.GOLD)
    holding = Holding.objects.create(account=account, asset=gram, quantity=Decimal("1"))

    assert holding.quantity_atomic == 1_000_000
    holding.asset = coin
    holding.save(update_fields=["asset"])
    holding.refresh_from_db()
    assert holding.quantity_atomic == 1


@pytest.mark.django_db
def test_iranian_quotes_store_whole_money_and_foreign_quotes_keep_precision(make_user):
    account = Account.objects.create(user=make_user(), name="Quote units")
    stock = Asset.objects.create(key="kama_stock", name="KAMA", asset_class=Asset.AssetClass.STOCK)
    bitcoin = Asset.objects.create(key="bitcoin_usd", name="Bitcoin", asset_class=Asset.AssetClass.CRYPTO)

    iranian = Price.objects.create(asset=stock, price=Decimal("100.5"))
    foreign = Price.objects.create(asset=bitcoin, price=Decimal("1.2345"))
    daily = DailyPriceAverage.objects.create(asset=stock, date="1405-07-03", avg_price=Decimal("100.5"))
    trade = LedgerEntry.objects.create(
        account=account, asset=stock, kind=LedgerEntry.Kind.BUY,
        quantity=Decimal("1"), price_tomans=Decimal("100.5"),
    )

    iranian.refresh_from_db()
    foreign.refresh_from_db()
    daily.refresh_from_db()
    trade.refresh_from_db()
    assert (iranian.price_iranian, iranian.price_foreign, iranian.price) == (Decimal("101"), None, Decimal("101"))
    assert (foreign.price_iranian, foreign.price_foreign, foreign.price) == (None, Decimal("1.2345"), Decimal("1.2345"))
    assert (daily.avg_price_iranian, daily.avg_price_foreign, daily.avg_price) == (Decimal("101"), None, Decimal("101"))
    assert (trade.price_iranian, trade.price_foreign, trade.price_tomans) == (Decimal("101"), None, Decimal("101"))
    payload = LedgerEntrySerializer(trade).data
    assert payload["unit_price_tomans"] == "101"
    assert payload["quantity"] == "1"


@pytest.mark.django_db
def test_foreign_seed_storage_precision_does_not_label_portfolio_quote_usd():
    bitcoin = Asset.objects.create(
        key="bitcoin_usd", name="Bitcoin", asset_class=Asset.AssetClass.CRYPTO,
    )
    row = Price.objects.create(
        asset=bitcoin, price=Decimal("5700000000.1234"),
        price_unit=Price.Unit.IRT, price_unit_verified=True,
    )

    assert row.price_foreign == Decimal("5700000000.1234")
    assert row.price_iranian is None
    assert AssetSerializer(bitcoin).data["quote_unit"] == "toman"


def test_ledger_admin_hides_padding_without_hiding_real_fraction():
    row = SimpleNamespace(quantity="250000.000000", amount_tomans="149500000.0000")
    admin = LedgerEntryAdmin.__new__(LedgerEntryAdmin)
    assert admin.quantity_display(row) == "250000"
    assert admin.amount_display(row) == "149500000"
    row.quantity = "0.000001"
    row.amount_tomans = "1.5000"
    assert admin.quantity_display(row) == "0.000001"
    assert admin.amount_display(row) == "2 (from 1.5000)"

"""Trade execution ledger — integration tests.

Integration type: `execute_trade` spans three models (Transaction, Holding,
Snapshot) inside one atomic transaction, so the value is in exercising them
together against the DB, not in isolated logic. These verify the balance math,
the ledger append, the immediate snapshot, and the oversell/guard rejections.
"""
from decimal import Decimal

import pytest

from portfolio.models import Account, Holding, Snapshot, Transaction
from portfolio.services.trades import (
    InsufficientHolding,
    ManualAssetTrade,
    TradeError,
    execute_trade,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def account(asset_catalog, make_user):
    user = make_user(email="trader@test.test")
    return Account.objects.create(user=user, name="Main")


def test_buy_creates_holding_ledger_and_snapshot(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    result = execute_trade(
        account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("3")
    )

    holding = Holding.objects.get(account=account, asset=asset_catalog["emami_coin"])
    assert holding.quantity == Decimal("3")
    txn = Transaction.objects.get(account=account)
    assert txn.side == "buy" and txn.quantity == Decimal("3")
    # Price is captured at execution.
    assert txn.price_tomans == Decimal("176000000.0000")
    # One snapshot stamped immediately (chart steps at the trade moment).
    assert Snapshot.objects.filter(user=account.user, account=None).count() == 1
    assert result["holding_quantity"] == "3"


def test_buy_accumulates_into_existing_holding(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("2"))
    execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("1.5"))
    holding = Holding.objects.get(account=account, asset=asset_catalog["emami_coin"])
    assert holding.quantity == Decimal("3.5")
    assert Transaction.objects.filter(account=account).count() == 2


def test_sell_reduces_holding(account, asset_catalog, write_prices):
    write_prices({"kama_stock": Decimal("1780")})
    execute_trade(account=account, asset=asset_catalog["kama_stock"], side="buy", quantity=Decimal("100000"))
    execute_trade(account=account, asset=asset_catalog["kama_stock"], side="sell", quantity=Decimal("40000"))
    holding = Holding.objects.get(account=account, asset=asset_catalog["kama_stock"])
    assert holding.quantity == Decimal("60000")


def test_full_sell_removes_holding_but_keeps_ledger(account, asset_catalog, write_prices):
    write_prices({"kama_stock": Decimal("1780")})
    execute_trade(account=account, asset=asset_catalog["kama_stock"], side="buy", quantity=Decimal("100"))
    execute_trade(account=account, asset=asset_catalog["kama_stock"], side="sell", quantity=Decimal("100"))
    assert not Holding.objects.filter(account=account, asset=asset_catalog["kama_stock"]).exists()
    # Ledger is append-only: both events survive the closed position.
    assert Transaction.objects.filter(account=account).count() == 2


def test_oversell_is_rejected_and_atomic(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("2"))
    with pytest.raises(InsufficientHolding):
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="sell", quantity=Decimal("5"))
    # The rejected sell wrote nothing: still one txn, holding unchanged.
    assert Transaction.objects.filter(account=account).count() == 1
    assert Holding.objects.get(account=account, asset=asset_catalog["emami_coin"]).quantity == Decimal("2")


def test_sell_with_no_holding_is_rejected(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    with pytest.raises(InsufficientHolding):
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="sell", quantity=Decimal("1"))


def test_house_asset_is_not_tradeable(account, asset_catalog):
    with pytest.raises(ManualAssetTrade):
        execute_trade(account=account, asset=asset_catalog["house_asset"], side="buy", quantity=Decimal("50"))


def test_non_positive_quantity_is_rejected(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    with pytest.raises(TradeError):
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("0"))


def test_bad_side_is_rejected(account, asset_catalog):
    with pytest.raises(TradeError):
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="hold", quantity=Decimal("1"))


def test_buy_without_price_records_zero_execution_price(account, asset_catalog):
    # No price written for this asset yet -> execution price 0, trade still valid.
    result = execute_trade(
        account=account, asset=asset_catalog["one_gram_coin"], side="buy", quantity=Decimal("4")
    )
    assert result["price_tomans"] == "0"
    assert Transaction.objects.get(account=account).price_tomans == Decimal("0")


class TestTradeEndpoint:
    """The HTTP surface: auth, ownership, and error mapping."""

    def _client(self, user):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_post_trade_creates_transaction(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        client = self._client(account.user)
        resp = client.post(
            f"/api/accounts/{account.id}/trades/",
            {"asset_key": "emami_coin", "side": "buy", "quantity": "2"},
            format="json",
        )
        assert resp.status_code == 201
        assert Decimal(resp.data["holding_quantity"]) == Decimal("2")

    def test_oversell_returns_400(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        client = self._client(account.user)
        resp = client.post(
            f"/api/accounts/{account.id}/trades/",
            {"asset_key": "emami_coin", "side": "sell", "quantity": "1"},
            format="json",
        )
        assert resp.status_code == 400

    def test_cannot_trade_in_another_users_account(self, account, asset_catalog, make_user):
        other = make_user(email="intruder@test.test")
        client = self._client(other)
        resp = client.post(
            f"/api/accounts/{account.id}/trades/",
            {"asset_key": "emami_coin", "side": "buy", "quantity": "1"},
            format="json",
        )
        assert resp.status_code == 404

    def test_snapshot_endpoint_returns_series_and_trades(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("2"))
        client = self._client(account.user)
        resp = client.get("/api/snapshots/")
        assert resp.status_code == 200
        assert "series" in resp.data and "trades" in resp.data
        assert len(resp.data["trades"]) == 1
        assert resp.data["trades"][0]["asset_key"] == "emami_coin"

    def test_tradeable_holding_cannot_bypass_ledger(self, account, asset_catalog):
        client = self._client(account.user)
        resp = client.post(
            f"/api/accounts/{account.id}/holdings/",
            {"asset_key": "emami_coin", "quantity": "2"},
            format="json",
        )
        assert resp.status_code == 400
        assert not account.holdings.exists()

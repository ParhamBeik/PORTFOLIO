"""Trade execution ledger — integration tests.

Integration type: `execute_trade` spans three models (Transaction, Holding,
Snapshot) inside one atomic transaction, so the value is in exercising them
together against the DB, not in isolated logic. These verify the balance math,
the ledger append, the immediate snapshot, and the oversell/guard rejections.
"""
import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from portfolio.models import Account, Holding, Snapshot, Transaction
from portfolio.services.trades import (
    InsufficientHolding,
    ManualAssetTrade,
    TradeError,
    execute_trade,
    undo_trade,
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


def test_buy_without_price_is_rejected_atomically(account, asset_catalog):
    with pytest.raises(TradeError, match="No valid execution price"):
        execute_trade(
            account=account,
            asset=asset_catalog["one_gram_coin"],
            side="buy",
            quantity=Decimal("4"),
        )
    assert not Transaction.objects.filter(account=account).exists()
    assert not Holding.objects.filter(account=account).exists()


def test_undo_latest_trade_reverses_holding_and_stamps_snapshots(
    account, asset_catalog, write_prices
):
    write_prices({"emami_coin": Decimal("176000000")})
    execute_trade(
        account=account,
        asset=asset_catalog["emami_coin"],
        side="buy",
        quantity=Decimal("3"),
    )
    trade = Transaction.objects.get(account=account)

    undo_trade(user=account.user, transaction_id=trade.id)

    assert not Transaction.objects.filter(pk=trade.id).exists()
    assert not Holding.objects.filter(
        account=account, asset=asset_catalog["emami_coin"]
    ).exists()
    assert Snapshot.objects.filter(user=account.user, account=None).count() == 2
    assert Snapshot.objects.filter(user=account.user, account=account).count() == 2


def test_undo_rejects_older_trade_for_same_asset(
    account, asset_catalog, write_prices
):
    write_prices({"emami_coin": Decimal("176000000")})
    execute_trade(
        account=account,
        asset=asset_catalog["emami_coin"],
        side="buy",
        quantity=Decimal("3"),
    )
    first = Transaction.objects.get(account=account)
    execute_trade(
        account=account,
        asset=asset_catalog["emami_coin"],
        side="sell",
        quantity=Decimal("1"),
    )

    with pytest.raises(TradeError):
        undo_trade(user=account.user, transaction_id=first.id)

    assert Transaction.objects.filter(account=account).count() == 2
    assert Holding.objects.get(
        account=account, asset=asset_catalog["emami_coin"]
    ).quantity == Decimal("2")


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

    def test_post_trade_preserves_historical_fields(self, account, asset_catalog):
        occurred_at = timezone.now() - datetime.timedelta(days=30)
        client = self._client(account.user)

        response = client.post(
            f"/api/accounts/{account.id}/trades/",
            {
                "asset_key": "emami_coin",
                "side": "buy",
                "quantity": "2",
                "timestamp": occurred_at.isoformat(),
                "price_tomans": "123456.75",
                "source": "imported",
            },
            format="json",
        )

        assert response.status_code == 201, response.data
        entry = Transaction.objects.get(account=account)
        assert entry.timestamp == occurred_at
        assert entry.price_tomans == Decimal("123456.7500")
        assert entry.source == "imported"

    def test_oversell_returns_400(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        client = self._client(account.user)
        resp = client.post(
            f"/api/accounts/{account.id}/trades/",
            {"asset_key": "emami_coin", "side": "sell", "quantity": "1"},
            format="json",
        )
        assert resp.status_code == 400

    def test_missing_execution_price_returns_400(self, account, asset_catalog):
        response = self._client(account.user).post(
            f"/api/accounts/{account.id}/trades/",
            {"asset_key": "one_gram_coin", "side": "buy", "quantity": "1"},
            format="json",
        )

        assert response.status_code == 400
        assert response.data["detail"] == "No valid execution price is available."
        assert not Transaction.objects.filter(account=account).exists()

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

    def test_holding_detail_rejects_wrong_parent_account(self, account, asset_catalog):
        other = Account.objects.create(user=account.user, name="Other")
        holding = Holding.objects.create(
            account=account,
            asset=asset_catalog["house_asset"],
            quantity=Decimal("50"),
        )
        response = self._client(account.user).patch(
            f"/api/accounts/{other.id}/holdings/{holding.id}/",
            {"quantity": "55"},
            format="json",
        )
        assert response.status_code == 404
        holding.refresh_from_db()
        assert holding.quantity == Decimal("50")

    def test_house_holding_rejects_nonpositive_price(self, account, asset_catalog):
        response = self._client(account.user).post(
            f"/api/accounts/{account.id}/holdings/",
            {"asset_key": "house_asset", "quantity": 0},
            format="json",
        )

        assert response.status_code == 400
        assert not account.holdings.exists()

    def test_duplicate_house_holding_returns_400(self, account, asset_catalog):
        Holding.objects.create(
            account=account,
            asset=asset_catalog["house_asset"],
            quantity=Decimal("50"),
        )
        response = self._client(account.user).post(
            f"/api/accounts/{account.id}/holdings/",
            {"asset_key": "house_asset", "quantity": "55"},
            format="json",
        )
        assert response.status_code == 400
        assert account.holdings.filter(asset=asset_catalog["house_asset"]).count() == 1

    def test_cannot_undo_another_users_trade(
        self, account, asset_catalog, write_prices, make_user
    ):
        write_prices({"emami_coin": Decimal("176000000")})
        execute_trade(
            account=account,
            asset=asset_catalog["emami_coin"],
            side="buy",
            quantity=Decimal("2"),
        )
        trade = Transaction.objects.get(account=account)

        response = self._client(make_user(email="other-trader@test.test")).delete(
            f"/api/transactions/{trade.id}/"
        )

        assert response.status_code == 404
        assert Transaction.objects.filter(pk=trade.id).exists()


def test_execute_trade_with_custom_price(account, asset_catalog):
    """Verify execute_trade respects custom passed prices."""
    result = execute_trade(
        account=account,
        asset=asset_catalog["emami_coin"],
        side="buy",
        quantity=Decimal("5"),
        price_tomans=Decimal("123456.789"),
    )
    holding = Holding.objects.get(account=account, asset=asset_catalog["emami_coin"])
    assert holding.quantity == Decimal("5")
    txn = Transaction.objects.get(account=account)
    assert txn.side == "buy" and txn.quantity == Decimal("5")
    assert txn.price_tomans == Decimal("123456.789")


def test_backfill_ledger_gap_command(account, asset_catalog, write_prices):
    """Verify that the backfill_ledger_gap command backfills transactions correctly."""
    Holding.objects.create(
        account=account,
        asset=asset_catalog["emami_coin"],
        quantity=Decimal("12.5"),
    )
    write_prices({"emami_coin": Decimal("20000000")})

    assert not Transaction.objects.filter(account=account, asset=asset_catalog["emami_coin"]).exists()

    from django.core.management import call_command

    # Dry-run should not create transactions.
    call_command("backfill_ledger_gap", "--price-source=zero")
    assert not Transaction.objects.filter(account=account, asset=asset_catalog["emami_coin"]).exists()

    # Commit with latest price.
    call_command("backfill_ledger_gap", "--price-source=latest", "--commit")
    txn = Transaction.objects.get(account=account, asset=asset_catalog["emami_coin"])
    assert txn.side == "buy"
    assert txn.quantity == Decimal("12.5")
    assert txn.price_tomans == Decimal("20000000")

    # Commit with manual price.
    Holding.objects.create(
        account=account,
        asset=asset_catalog["kama_stock"],
        quantity=Decimal("100"),
    )
    call_command("backfill_ledger_gap", "--price-source=manual", "--price=55.5", "--commit")
    txn2 = Transaction.objects.get(account=account, asset=asset_catalog["kama_stock"])
    assert txn2.side == "buy"
    assert txn2.quantity == Decimal("100")
    assert txn2.price_tomans == Decimal("55.5")

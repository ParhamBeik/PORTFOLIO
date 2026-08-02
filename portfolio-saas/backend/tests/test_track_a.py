import pytest
from decimal import Decimal
from django.utils import timezone
import jdatetime
import datetime
from django.core.management import call_command
from rest_framework.exceptions import ValidationError

from portfolio.models import Account, Asset, Holding, Transaction
from portfolio.services.trades import execute_trade, undo_trade
from portfolio.serializers import TradeInputSerializer
from portfolio.services.timeline import holdings_as_of, twr, xirr, asset_metrics
from marketdata.models import MarketCandle, GoldCurrencyHistory

@pytest.mark.django_db
class TestTrackA:
    def test_reconcile_ledger_clean(self, django_user_model):
        """Unit test for reconcile_ledger command when ledger is clean."""
        from portfolio.services.ledger import create_ledger_entry
        from portfolio.models import LedgerEntry

        user = django_user_model.objects.create_user(email="test@example.com", password="password123")
        acc = Account.objects.create(user=user, name="Main")
        asset = Asset.objects.create(key="test_asset", name="Test", is_active=True)
        create_ledger_entry(
            account=acc, kind=LedgerEntry.Kind.OPENING_CASH, amount_tomans="100000",
        )
        execute_trade(account=acc, asset=asset, side="buy", quantity="10.0", price_tomans="1000")

        call_command("reconcile_ledger")

    def test_reconcile_ledger_drift(self, django_user_model):
        """Unit test for reconcile_ledger command when ledger drifts."""
        from portfolio.services.ledger import create_ledger_entry
        from portfolio.models import LedgerEntry

        user = django_user_model.objects.create_user(email="test2@example.com", password="password123")
        acc = Account.objects.create(user=user, name="Main")
        asset = Asset.objects.create(key="test_asset", name="Test", is_active=True)
        create_ledger_entry(
            account=acc, kind=LedgerEntry.Kind.OPENING_CASH, amount_tomans="100000",
        )
        execute_trade(account=acc, asset=asset, side="buy", quantity="10.0", price_tomans="1000")

        h = Holding.objects.get(account=acc, asset=asset)
        h.quantity = Decimal("12.0")
        h.save()

        with pytest.raises(SystemExit):
            call_command("reconcile_ledger")

    def test_reconcile_ledger_fix_rebuilds_cash_and_holdings(self, django_user_model):
        from portfolio.services.ledger import create_ledger_entry
        from portfolio.models import LedgerEntry

        user = django_user_model.objects.create_user(email="fix@example.com", password="password123")
        acc = Account.objects.create(user=user, name="Main")
        asset = Asset.objects.create(key="fix_asset", name="Fix", is_active=True)
        create_ledger_entry(
            account=acc, kind=LedgerEntry.Kind.OPENING_CASH, amount_tomans="50000",
        )
        execute_trade(account=acc, asset=asset, side="buy", quantity="5", price_tomans="1000")
        acc.cash_balance_tomans = Decimal("1")
        acc.save(update_fields=["cash_balance_tomans"])
        Holding.objects.filter(account=acc).update(quantity=Decimal("99"))

        call_command("reconcile_ledger", "--fix")
        acc.refresh_from_db()
        assert acc.cash_balance_tomans == Decimal("45000")
        assert Holding.objects.get(account=acc, asset=asset).quantity == Decimal("5")

    def test_holdings_as_of(self, django_user_model):
        """Integration test for holdings_as_of backwards calculation."""
        user = django_user_model.objects.create_user(email="test3@example.com", password="password123")
        acc = Account.objects.create(user=user, name="Main")
        asset = Asset.objects.create(key="test_asset", name="Test", is_active=True)
        
        t1 = timezone.now() - datetime.timedelta(days=2)
        t2 = timezone.now() - datetime.timedelta(days=1)
        
        # Create transactions manually for precise timestamps
        Transaction.objects.create(account=acc, asset=asset, side="buy", quantity=Decimal("10"), price_tomans=1000, timestamp=t1)
        Transaction.objects.create(account=acc, asset=asset, side="sell", quantity=Decimal("4"), price_tomans=1100, timestamp=t2)
        Holding.objects.create(account=acc, asset=asset, quantity=Decimal("6"))
        
        # as of day 0 (now) -> 6
        assert holdings_as_of(user, acc, timezone.now()) == {"test_asset": Decimal("6.0")}
        
        # as of day -1.5 -> 10
        mid_date = timezone.now() - datetime.timedelta(days=1, hours=12)
        assert holdings_as_of(user, acc, mid_date) == {"test_asset": Decimal("10.0")}
        
        # as of day -3 -> 0
        old_date = timezone.now() - datetime.timedelta(days=3)
        assert holdings_as_of(user, acc, old_date) == {}
        
    def test_serializer_validation_future_date(self):
        """Unit test for rejecting future dates."""
        asset = Asset.objects.create(key="test", name="Test", is_active=True, asset_class="Stock", tse_symbol="FOO")
        
        data = {
            "asset_key": "test",
            "side": "buy",
            "quantity": "1.0",
            "timestamp": (timezone.now() + datetime.timedelta(days=1)).isoformat()
        }
        serializer = TradeInputSerializer(data=data)
        assert not serializer.is_valid()
        assert "timestamp" in serializer.errors

    def test_serializer_validation_before_history(self):
        """Unit test for rejecting dates before history."""
        asset = Asset.objects.create(key="test", name="Test", is_active=True, asset_class="Stock", tse_symbol="FOO")
        
        j_date_str = "1403-01-01"
        MarketCandle.objects.create(symbol="FOO", timeframe="1d_unadj", date_time=j_date_str + " 00:00:00", close_price=100)
        
        # Try a date before history (1399)
        dt = jdatetime.date(1399, 1, 1).togregorian()
        data = {
            "asset_key": "test",
            "side": "buy",
            "quantity": "1.0",
            "timestamp": datetime.datetime(dt.year, dt.month, dt.day, tzinfo=datetime.timezone.utc).isoformat()
        }
        
        serializer = TradeInputSerializer(data=data)
        assert not serializer.is_valid()
        assert "timestamp" in serializer.errors


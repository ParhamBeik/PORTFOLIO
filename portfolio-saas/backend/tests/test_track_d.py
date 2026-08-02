import datetime
from decimal import Decimal
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from portfolio.models import Account, BacktestRun, BacktestUserQuota, Watchlist, WatchlistItem, Snapshot, Transaction, Asset
from marketdata.models import MarketInstrument, SymbolIntegrity, MarketCandle, GoldCurrencyHistory
from portfolio.tasks import run_backtest_task

User = get_user_model()

class TestTrackD(APITestCase):

    def setUp(self):
        # Create users
        self.pro_group, _ = Group.objects.get_or_create(name="Pro")
        
        self.pro_user = User.objects.create_user(email="pro@example.com", password="password", tier="PRO")
        self.pro_user.groups.add(self.pro_group)
        
        self.free_user = User.objects.create_user(email="free@example.com", password="password")
        
        # Setup Account
        self.pro_account = Account.objects.create(user=self.pro_user, name="Pro Main")
        self.free_account = Account.objects.create(user=self.free_user, name="Free Main")
        
        # Seed simple active assets
        self.kama = Asset.objects.create(key="kama_stock", name="Kama", tse_symbol="KAMA", asset_class="Stock", is_active=True)
        self.usd = Asset.objects.create(key="usd_cash", name="USD", brs_symbol="USD", asset_class="Cash", is_active=True)
        self.emami = Asset.objects.create(key="emami_coin", name="Emami", brs_symbol="Emami", asset_class="Gold", is_active=True)
        
        SymbolIntegrity.objects.create(symbol="KAMA", passes_gate=True)
        SymbolIntegrity.objects.create(symbol="USD", passes_gate=True)
        SymbolIntegrity.objects.create(symbol="Emami", passes_gate=True)

    def backtest_payload(self):
        """The account-scoped backtest request contract."""
        return {
            "account_id": self.pro_account.id,
            "universe_mode": "portfolio",
            "symbols": ["kama_stock", "usd_cash", "emami_coin"],
            "basis": "nominal_toman",
            "completed_years": 5,
        }

    def test_backtest_quota_limits(self):
        # Testing Protocol: We choose an integration test for the backtest quota endpoint to verify that the HTTP API correctly gates and rejects requests exceeding the daily quota of walk-forward simulation runs.
        self.client.force_authenticate(user=self.pro_user)
        
        # We set settings.DAILY_BACKTEST_LIMIT to 2 for testing
        old_limit = getattr(settings, "DAILY_BACKTEST_LIMIT", 10)
        settings.DAILY_BACKTEST_LIMIT = 2
        try:
            # 1. Run 1
            res = self.client.post(reverse("backtest"), self.backtest_payload(), format="json")
            assert res.status_code == status.HTTP_201_CREATED
            
            # 2. Run 2
            res = self.client.post(reverse("backtest"), self.backtest_payload(), format="json")
            assert res.status_code == status.HTTP_201_CREATED
            
            # 3. Run 3 -> Should fail with 429
            res = self.client.post(reverse("backtest"), self.backtest_payload(), format="json")
            assert res.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        finally:
            settings.DAILY_BACKTEST_LIMIT = old_limit

    def test_backtest_celery_boundary_checks(self):
        # Testing Protocol: We choose an integration test for Celery queue boundaries to verify that enqueued backtests are correctly marked as failed if the submitting user is not Pro or has exceeded their daily limit.
        # Create a run for a free user
        run1 = BacktestRun.objects.create(
            user=self.free_user,
            basis="nominal",
            universe=["kama_stock", "usd_cash", "emami_coin"],
            universe_hash="hash",
            params_hash="hash",
            status=BacktestRun.Status.QUEUED,
        )
        
        run_backtest_task(run1.id)
        run1.refresh_from_db()
        assert run1.status == BacktestRun.Status.FAILED
        assert "Pro subscription" in run1.error

        # Over-quota run for Pro user
        today = timezone.now().date()
        quota = BacktestUserQuota.objects.create(user=self.pro_user, day=today, count=999)
        
        run2 = BacktestRun.objects.create(
            user=self.pro_user,
            basis="nominal",
            universe=["kama_stock", "usd_cash", "emami_coin"],
            universe_hash="hash",
            params_hash="hash",
            status=BacktestRun.Status.QUEUED,
        )
        run_backtest_task(run2.id)
        run2.refresh_from_db()
        assert run2.status == BacktestRun.Status.FAILED
        assert "limit" in run2.error

    def test_watchlist_operations(self):
        # Testing Protocol: We choose an integration test for watchlist management to assert that users can add, fetch, and delete curated symbols with force-include and force-exclude constraints.
        self.client.force_authenticate(user=self.pro_user)
        
        # POST to add symbol with force_include
        res = self.client.post(reverse("watchlist"), {"symbol": "KAMA", "force_include": True})
        assert res.status_code == status.HTTP_200_OK
        assert res.data["symbol"] == "KAMA"
        assert res.data["force_include"] is True
        
        # GET to verify list
        res = self.client.get(reverse("watchlist"))
        assert res.status_code == status.HTTP_200_OK
        assert len(res.data["items"]) == 1
        assert res.data["items"][0]["symbol"] == "KAMA"
        
        # POST to delete
        res = self.client.post(reverse("watchlist"), {"symbol": "KAMA", "delete": True})
        assert res.status_code == status.HTTP_200_OK
        
        # GET to verify it is gone
        res = self.client.get(reverse("watchlist"))
        assert len(res.data["items"]) == 0

    # `/api/performance/` is now account-scoped and ledger-derived; its contract
    # lives in tests/test_ledger_api.py (opening baseline, external cash flows,
    # account scoping). The old snapshot-delta assertions were removed with it.

    def test_data_integrity_endpoint(self):
        # Testing Protocol: We choose an integration test for the integrity API endpoint to verify that only authenticated staff users can access data quality reports.
        self.client.force_authenticate(user=self.pro_user)
        res = self.client.get(reverse("integrity"))
        assert res.status_code == status.HTTP_403_FORBIDDEN
        
        # Authenticate as staff
        self.pro_user.is_staff = True
        self.pro_user.save()
        
        res = self.client.get(reverse("integrity"))
        assert res.status_code == status.HTTP_200_OK
        # We seeded 3 SymbolIntegrity rows in setUp
        assert len(res.data["integrity"]) == 3

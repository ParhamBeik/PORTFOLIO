from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from portfolio.models import Account, Asset
from marketdata.models import SymbolIntegrity

User = get_user_model()

class TestTrackD(APITestCase):

    def setUp(self):
        # Create users
        self.pro_group, _ = Group.objects.get_or_create(name="Pro")
        
        self.pro_user = User.objects.create_user(email="pro@example.com", password="password")
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

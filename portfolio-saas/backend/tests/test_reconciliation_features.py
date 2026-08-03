import pytest
from decimal import Decimal
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from portfolio.models import Account, Asset, Holding, Liability
from marketdata.models import GoldCurrencyHistory, ArchiveFetchState


@pytest.fixture
def auth_client(db, make_user):
    user = make_user(email="admin@test.test", tier=User.Tier.PRO)
    user.is_staff = True
    user.save()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def test_liability_netting_in_valuation(db, make_user):
    user = make_user(email="user@test.test")
    account = Account.objects.create(name="Test Account", user=user)
    
    asset = Asset.objects.create(
        key="gold_18k_gram", name="Gold 18k", asset_class="Gold", currency="IRT"
    )
    # Create holding
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("10"))
    
    # Valuation without liabilities: 10 * 100_000 = 1,000,000
    prices = {"gold_18k_gram": Decimal("100000")}
    
    from portfolio.services.valuation import value_account
    val = value_account(account, prices)
    assert val["total"] == Decimal("1000000")
    
    # Create liability
    Liability.objects.create(
        account=account,
        label="Test Loan",
        amount_tomans=Decimal("300000"),
    )
    
    # Valuation with liabilities: 1,000,000 - 300,000 = 700,000
    val = value_account(account, prices)
    assert val["total"] == Decimal("700000")
    assert val["total_liabilities"] == 300000.0


def test_usdt_basis_conversion_fallback(db):
    from portfolio.services.deflator import to_basis
    
    # Seed historical rate for USD only
    GoldCurrencyHistory.objects.create(
        symbol="USD", date="1405-01-01", close_price=Decimal("50000")
    )
    
    import pandas as pd
    series = pd.Series([100000.0], index=[pd.Timestamp("2026-03-21", tz="UTC")])
    
    # Convert using usdt_denominated basis. Since USDT_IRT is missing, it should fallback to USD.
    res = to_basis(series, "usdt_denominated")
    assert not res.isna().all()
    assert res.iloc[0] == 2.0  # 100,000 / 50,000 = 2.0


def test_admin_endpoints(auth_client, db):
    client, user = auth_client
    
    # Test AdminUserListView
    url = reverse("admin-users")
    response = client.get(url)
    assert response.status_code == status.HTTP_200_OK
    assert len(response.data) >= 1
    
    # Test RetryArchiveJobView
    state = ArchiveFetchState.objects.create(
        symbol="FOO",
        endpoint="announcements",
        stored_rows=10,
        expected_rows=20,
        consecutive_failures=3,
        last_error="Temporary network issue."
    )
    
    retry_url = reverse("admin-retry-job", kwargs={"job_id": state.id})
    response = client.post(retry_url)
    assert response.status_code == status.HTTP_200_OK
    assert response.data["status"] == "queued"
    
    # Check that failures were reset
    state.refresh_from_db()
    assert state.consecutive_failures == 0

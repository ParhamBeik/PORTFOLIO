import pytest
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from rest_framework.test import APIClient
from portfolio.models import Account, Asset, Holding, Snapshot

@pytest.mark.django_db
def test_snapshot_daily_grouping_and_7d_pinning(make_user):
    user = make_user("chart_test_user@example.com")
    client = APIClient()
    client.force_authenticate(user=user)

    account = Account.objects.create(user=user, name="Test Account")
    asset = Asset.objects.create(key="test_gold", name="Gold Asset", asset_class=Asset.AssetClass.GOLD, is_active=True)
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("10"))

    # Create 2 snapshots today (spanning only hours of today)
    now = timezone.now()
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("100000"), timestamp=now - timedelta(hours=3))
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("105000"), timestamp=now - timedelta(hours=1))

    # Query 7-day snapshot series (returns fine-grained 2-minute time grid points)
    res = client.get("/api/snapshots/?days=7")
    assert res.status_code == 200
    data = res.json()

    assert "series" in data
    series = data["series"]

    # Must contain fine-grained 2-minute grid points (> 500 points for 7 days)
    assert len(series) > 500

    # Query 365-day snapshot series (resampled to daily entries)
    res_year = client.get("/api/snapshots/?days=365")
    assert res_year.status_code == 200
    data_year = res_year.json()

    assert "series" in data_year
    series_year = data_year["series"]
    assert len(series_year) >= 365

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

    # Query 7-day snapshot series
    res = client.get("/api/snapshots/?days=7")
    assert res.status_code == 200
    data = res.json()

    assert "series" in data
    series = data["series"]

    # Must contain 7 daily entries, not just 2 entries from today
    assert len(series) == 7

    # Check dates are distinct calendar days
    dates = [item.get("date") for item in series if item.get("date")]
    assert len(dates) == 7
    assert len(set(dates)) == 7

"""Net-worth history: one point per calendar day, averaged across that day's
fetches (fetchers run every 2 minutes; "today" is the running average so far).
"""
import pytest
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from rest_framework.test import APIClient
from portfolio.models import Account, Asset, Holding, Snapshot


@pytest.mark.django_db
def test_same_day_snapshots_collapse_to_one_averaged_point(make_user):
    user = make_user("chart_test_user@example.com")
    client = APIClient()
    client.force_authenticate(user=user)

    account = Account.objects.create(user=user, name="Test Account")
    asset = Asset.objects.create(key="test_gold", name="Gold Asset", asset_class=Asset.AssetClass.GOLD, is_active=True)
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("10"))

    now = timezone.now()
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("100000"), timestamp=now - timedelta(hours=3))
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("105000"), timestamp=now - timedelta(hours=1))
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("110000"), timestamp=now - timedelta(minutes=5))

    res = client.get(f"/api/snapshots/?days=7&account={account.id}")
    assert res.status_code == 200
    series = res.json()["series"]

    assert len(series) == 1
    assert series[0]["date"] == now.strftime("%Y-%m-%d")
    assert Decimal(series[0]["total"]) == Decimal("105000")  # mean of the three


@pytest.mark.django_db
def test_snapshots_across_multiple_days_yield_one_point_per_day(make_user):
    user = make_user("chart_multi_day@example.com")
    account = Account.objects.create(user=user, name="Test Account")
    now = timezone.now()

    for offset_days, values in enumerate([[100000, 102000], [200000], [300000, 301000, 299000]]):
        day = now - timedelta(days=offset_days)
        for i, value in enumerate(values):
            Snapshot.objects.create(
                user=user, account=account, total_value_tomans=Decimal(str(value)),
                timestamp=day - timedelta(hours=i),
            )

    client = APIClient()
    client.force_authenticate(user=user)
    res = client.get(f"/api/snapshots/?days=7&account={account.id}")
    assert res.status_code == 200
    series = res.json()["series"]

    assert len(series) == 3
    # Oldest first.
    assert series[0]["date"] < series[1]["date"] < series[2]["date"]


@pytest.mark.django_db
def test_days_all_returns_history_beyond_one_year(make_user):
    user = make_user("chart_all_range@example.com")
    account = Account.objects.create(user=user, name="Test Account")
    old_snapshot_time = timezone.now() - timedelta(days=800)
    Snapshot.objects.create(
        user=user, account=account, total_value_tomans=Decimal("50000"), timestamp=old_snapshot_time,
    )

    client = APIClient()
    client.force_authenticate(user=user)

    capped = client.get(f"/api/snapshots/?days=365&account={account.id}").json()["series"]
    assert capped == []

    full = client.get(f"/api/snapshots/?days=all&account={account.id}").json()["series"]
    assert len(full) == 1
    assert full[0]["date"] == old_snapshot_time.strftime("%Y-%m-%d")


@pytest.mark.django_db
def test_snapshot_series_does_not_fabricate_pre_history(make_user):
    user = make_user("chart_start@example.com")
    snapshot_time = timezone.now() - timedelta(days=1)
    Snapshot.objects.create(
        user=user,
        account=None,
        total_value_tomans=Decimal("100000"),
        timestamp=snapshot_time,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    series = client.get("/api/snapshots/?days=30").json()["series"]

    assert series[0]["date"] == snapshot_time.strftime("%Y-%m-%d")
    assert len(series) == 1


@pytest.mark.django_db
def test_prune_snapshots_disabled_by_default_deletes_nothing(make_user, settings):
    from portfolio.services.maintenance import prune_snapshots

    settings.SNAPSHOT_PRUNE_ENABLED = False
    settings.SNAPSHOT_RETENTION_DAYS = 30
    user = make_user("prune_test@example.com")
    account = Account.objects.create(user=user, name="Test Account")
    old_time = timezone.now() - timedelta(days=90)
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("10000"), timestamp=old_time)

    before = Snapshot.objects.count()
    result = prune_snapshots()
    after = Snapshot.objects.count()

    assert result["enabled"] is False
    assert after == before

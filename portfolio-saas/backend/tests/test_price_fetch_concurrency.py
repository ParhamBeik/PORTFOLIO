"""Integration tests for run_price_fetch Redis lock concurrency and downtime gap backfill tagging.
"""
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
import pytest
from unittest.mock import MagicMock

from accounts.models import User
from portfolio.models import Account, Price, Snapshot
from portfolio.tasks import run_price_fetch


@pytest.mark.django_db
def test_run_price_fetch_concurrency_lock(asset_catalog, raw_market_sample, monkeypatch):
    """Verify that a second run_price_fetch call fails to run concurrently if a Redis lock is held."""
    import portfolio.tasks as mod
    monkeypatch.setattr(mod, "fetch_all_markets", lambda _settings: raw_market_sample)

    # Mock get_redis to return a fake Redis client that simulates locking
    mock_redis = MagicMock()
    # First call to set (nx=True) returns True (success), second returns False (locked)
    mock_redis.set.side_effect = [True, False]
    monkeypatch.setattr(mod, "get_redis", lambda: mock_redis)

    # First fetch succeeds
    res1 = run_price_fetch()
    assert res1["written"] is True

    # Second concurrent fetch gets blocked by lock and does not write
    res2 = run_price_fetch()
    assert res2["written"] is False
    assert res2["priced"] == {}
    mock_redis.eval.assert_called_once()


@pytest.mark.django_db
def test_run_price_fetch_downtime_gap_tagging(asset_catalog, raw_market_sample, monkeypatch):
    """Verify that snapshots generated for downtime gaps are tagged with is_estimated=True."""
    import portfolio.tasks as mod
    monkeypatch.setattr(mod, "fetch_all_markets", lambda _settings: raw_market_sample)
    # Disable Redis during this test to avoid lock interference
    monkeypatch.setattr(mod, "get_redis", lambda: None)

    user = User.objects.create_user(email="gap@test.test", password="Sup3rSecret!")
    account = Account.objects.create(user=user, name="Main")

    # Seed one old snapshot to establish a downtime gap
    old_time = timezone.now() - timedelta(minutes=10)
    # Use update() to bypass auto_now_add=True restriction
    snap1 = Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("1000"))
    snap2 = Snapshot.objects.create(user=user, account=None, total_value_tomans=Decimal("1000"))
    Snapshot.objects.filter(id__in=[snap1.id, snap2.id]).update(timestamp=old_time)

    # Execute fetch (will detect gap and backfill missing intervals)
    res = run_price_fetch()
    assert res["written"] is True

    # Check that the backfilled snapshots are marked as estimated
    estimated_snaps = Snapshot.objects.filter(user=user, is_estimated=True)
    assert estimated_snaps.exists()
    assert estimated_snaps.values("timestamp").distinct().count() > 1
    assert estimated_snaps.order_by("timestamp").first().timestamp < timezone.now() - timedelta(minutes=2)
    
    # Real current snapshot must not be estimated
    current_snaps = Snapshot.objects.filter(user=user, is_estimated=False).order_by("-timestamp")
    # There should be 4: the original 2 (one account, one user) + 2 new ones (one account, one user)
    assert current_snaps.count() == 4

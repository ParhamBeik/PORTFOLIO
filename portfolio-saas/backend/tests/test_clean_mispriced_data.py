"""Tests for the clean_mispriced_data management command and its core logic.

Unit/integration tests (DB-backed but no HTTP layer) exercising
`audit_and_repair_prices` directly: this is pure business logic with a few
collaborators (ORM models), so a narrow DB-integration test is the right
fit on the pyramid — fast enough to run every commit, but real enough to
catch the `KeyError` and cross-user contamination bugs a mocked DB would
hide.
"""
from decimal import Decimal

import pytest
from django.core.management import call_command

from portfolio.management.commands.clean_mispriced_data import audit_and_repair_prices
from portfolio.models import Account, Asset, Price, Snapshot


def test_dry_run_cli_does_not_raise(asset_catalog):
    """Reproduces audit finding #1: `options['dry-run']` used to KeyError on any invocation."""
    call_command("clean_mispriced_data", "--dry-run")
    call_command("clean_mispriced_data")  # bare invocation is also dry-run by default


def test_fix_and_dry_run_are_mutually_exclusive(asset_catalog):
    with pytest.raises(Exception):
        call_command("clean_mispriced_data", "--fix", "--dry-run")


def test_flagged_spike_is_not_folded_into_baseline(asset_catalog, db):
    """A single bad spike must not corrupt the baseline for the next (correct) price."""
    asset = asset_catalog["emami_coin"]
    for price in [Decimal("1000000"), Decimal("1010000"), Decimal("1005000")]:
        Price.objects.create(asset=asset, price=price, source="TEST")
    spike = Price.objects.create(asset=asset, price=Decimal("5000000"), source="TEST")  # bogus spike
    recovery = Price.objects.create(asset=asset, price=Decimal("1015000"), source="TEST")  # correct, back to normal

    stats = audit_and_repair_prices(fix=True)

    assert stats["flagged_spikes"] == 1
    assert not Price.objects.filter(id=spike.id).exists()
    assert Price.objects.filter(id=recovery.id).exists()  # must survive: it's not a spike vs. the real baseline


def test_snapshot_purge_is_scoped_per_user(asset_catalog, db):
    """Audit finding #2: a global median let one whale account nuke another user's legit snapshots."""
    from accounts.models import User

    whale = User.objects.create_user(email="whale@test.test", password="Sup3rSecret!")
    normal = User.objects.create_user(email="normal@test.test", password="Sup3rSecret!")
    whale_account = Account.objects.create(user=whale, name="Main")
    normal_account = Account.objects.create(user=normal, name="Main")

    # Whale: legit history clusters around 10,000,000,000 Tomans.
    for _ in range(6):
        Snapshot.objects.create(user=whale, account=whale_account, total_value_tomans=Decimal("10000000000"))
    whale_outlier = Snapshot.objects.create(
        user=whale, account=whale_account, total_value_tomans=Decimal("100000000000")  # 10x median -> corrupt
    )

    # Normal user: legit history clusters around 50,000,000 Tomans — far below
    # the whale's median, so a global-median filter would wrongly flag these.
    normal_snaps = [
        Snapshot.objects.create(user=normal, account=normal_account, total_value_tomans=Decimal("50000000"))
        for _ in range(6)
    ]

    stats = audit_and_repair_prices(fix=True)

    assert stats["purged_snapshots"] == 1
    assert not Snapshot.objects.filter(id=whale_outlier.id).exists()
    for snap in normal_snaps:
        assert Snapshot.objects.filter(id=snap.id).exists()

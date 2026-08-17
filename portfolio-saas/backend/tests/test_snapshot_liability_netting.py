"""The net-worth series must be ONE measure end to end.

Migration 0027 rewrites pre-netting snapshots so the chart stops showing a cliff
that no portfolio experienced. These tests exercise the detection and arithmetic
directly, because a migration that rewrites financial history and is only ever
run once is exactly the code that needs a test standing beside it.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from portfolio.models import Account, Liability, Snapshot

pytestmark = pytest.mark.django_db

MIGRATION = "portfolio.migrations.0027_backfill_snapshot_liability_netting"


def _forwards():
    """Run the migration's forwards() against the live test schema."""
    import importlib

    from django.apps import apps

    module = importlib.import_module(MIGRATION)
    with connection.schema_editor(atomic=False) as editor:
        module.forwards(apps, editor)
    return module


def _seed(user, account, *, gross, net, liability):
    """A series that is gross, then steps down to net at a single boundary."""
    Liability.objects.create(
        account=account, label="Mortgage", amount_tomans=Decimal(liability)
    )
    start = timezone.now() - timedelta(hours=6)
    rows = []
    for index in range(4):  # pre-changeover: gross
        rows.append(Snapshot(
            user=user, account=account, total_value_tomans=Decimal(gross),
            timestamp=start + timedelta(minutes=2 * index),
        ))
        rows.append(Snapshot(
            user=user, account=None, total_value_tomans=Decimal(gross),
            timestamp=start + timedelta(minutes=2 * index),
        ))
    for index in range(4, 8):  # post-changeover: already net
        rows.append(Snapshot(
            user=user, account=account, total_value_tomans=Decimal(net),
            timestamp=start + timedelta(minutes=2 * index),
        ))
        rows.append(Snapshot(
            user=user, account=None, total_value_tomans=Decimal(net),
            timestamp=start + timedelta(minutes=2 * index),
        ))
    Snapshot.objects.bulk_create(rows)


def test_pre_netting_rows_are_rewritten_and_the_cliff_disappears(make_user):
    user = make_user(email=f"n{__import__('uuid').uuid4().hex[:8]}@test.test")
    account = Account.objects.create(user=user, name="Main")
    _seed(user, account, gross="1900000000", net="300000000", liability="1600000000")

    _forwards()

    values = list(
        Snapshot.objects.filter(account=account)
        .order_by("timestamp")
        .values_list("total_value_tomans", flat=True)
    )
    # Every row now sits on the net definition, so no step remains.
    assert set(values) == {Decimal("300000000.0000")}
    biggest_step = max(
        abs(values[i] - values[i - 1]) for i in range(1, len(values))
    )
    assert biggest_step == 0

    # The user-level rollup is netted by the same amount.
    rollup = set(
        Snapshot.objects.filter(account__isnull=True)
        .values_list("total_value_tomans", flat=True)
    )
    assert rollup == {Decimal("300000000.0000")}


def test_a_series_with_no_changeover_is_left_alone(make_user):
    """An account whose history is already consistent must not be double-netted."""
    user = make_user(email=f"n{__import__('uuid').uuid4().hex[:8]}@test.test")
    account = Account.objects.create(user=user, name="Main")
    Liability.objects.create(
        account=account, label="Mortgage", amount_tomans=Decimal("1600000000")
    )
    start = timezone.now() - timedelta(hours=2)
    Snapshot.objects.bulk_create([
        Snapshot(user=user, account=account, total_value_tomans=Decimal("300000000"),
                 timestamp=start + timedelta(minutes=2 * i))
        for i in range(6)
    ])

    _forwards()

    assert set(
        Snapshot.objects.filter(account=account)
        .values_list("total_value_tomans", flat=True)
    ) == {Decimal("300000000.0000")}


def test_an_account_without_liabilities_is_untouched(make_user):
    user = make_user(email=f"n{__import__('uuid').uuid4().hex[:8]}@test.test")
    plain = Account.objects.create(user=user, name="No debt")
    start = timezone.now() - timedelta(hours=2)
    # A genuine large drop, from a sale rather than a netting change.
    Snapshot.objects.bulk_create([
        Snapshot(user=user, account=plain, total_value_tomans=Decimal("2000000000"),
                 timestamp=start),
        Snapshot(user=user, account=plain, total_value_tomans=Decimal("100000000"),
                 timestamp=start + timedelta(minutes=2)),
    ])

    _forwards()

    assert list(
        Snapshot.objects.filter(account=plain)
        .order_by("timestamp")
        .values_list("total_value_tomans", flat=True)
    ) == [Decimal("2000000000.0000"), Decimal("100000000.0000")]


def test_the_rewrite_is_reversible(make_user):
    user = make_user(email=f"n{__import__('uuid').uuid4().hex[:8]}@test.test")
    account = Account.objects.create(user=user, name="Main")
    _seed(user, account, gross="1900000000", net="300000000", liability="1600000000")
    before = list(
        Snapshot.objects.order_by("id").values_list("id", "total_value_tomans")
    )

    module = _forwards()
    assert list(
        Snapshot.objects.order_by("id").values_list("id", "total_value_tomans")
    ) != before

    from django.apps import apps

    with connection.schema_editor(atomic=False) as editor:
        module.backwards(apps, editor)

    assert list(
        Snapshot.objects.order_by("id").values_list("id", "total_value_tomans")
    ) == before


def test_negative_net_worth_is_kept_not_clamped(make_user):
    """Liabilities exceeding assets is a real state, not something to floor at 0."""
    user = make_user(email=f"n{__import__('uuid').uuid4().hex[:8]}@test.test")
    account = Account.objects.create(user=user, name="Underwater")
    _seed(user, account, gross="500000000", net="-1100000000", liability="1600000000")

    _forwards()

    assert Snapshot.objects.filter(
        account=account, total_value_tomans__lt=0
    ).exists()

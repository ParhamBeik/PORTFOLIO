"""A house must be worth what it was worth at the time, not what it is worth now.

Integration tests: the behaviour spans LedgerEntry rows, the holding projection
and the as-of valuation, so it only reproduces against the database.

Before dated marks existed, revaluing a house REPLACED its single opening entry.
The house therefore carried one price across all of history: every rial of
appreciation was invisible to the net-worth chart, and today's price was baked
into the opening balance, which understated TWR. Since real estate is a large
share of this family's net worth, that distortion was material.
"""
import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from portfolio.models import Account, Asset, Holding, LedgerEntry
from portfolio.services.ledger import record_house_mark
from portfolio.services.timeline import (
    house_area_as_of,
    house_marks_as_of,
    holdings_as_of,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def house_account(asset_catalog, make_user):
    user = make_user(email="house-marks@test.test")
    account = Account.objects.create(user=user, name="Home")
    return user, account, Asset.objects.get(key="house_asset")


def test_first_mark_is_the_opening_position(house_account):
    user, account, house = house_account

    entry = record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("90"), area_sqm=Decimal("90.2"),
        occurred_at=timezone.now() - dt.timedelta(days=400),
    )

    assert entry.kind == LedgerEntry.Kind.OPENING_POSITION


def test_later_marks_append_and_do_not_overwrite_history(house_account):
    user, account, house = house_account
    old = timezone.now() - dt.timedelta(days=400)
    recent = timezone.now() - dt.timedelta(days=10)

    record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("90"), area_sqm=Decimal("90.2"), occurred_at=old,
    )
    second = record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("150"), area_sqm=Decimal("90.2"), occurred_at=recent,
    )

    assert second.kind == LedgerEntry.Kind.VALUATION_MARK
    # Both events survive; the revaluation did not rewrite the opening.
    assert LedgerEntry.objects.filter(account=account, asset=house).count() == 2
    # The old date still sees the old price -- this is the whole point.
    assert house_marks_as_of(account, old)["house_asset"] == Decimal("90")
    assert house_marks_as_of(account, recent)["house_asset"] == Decimal("150")
    # Before any mark exists the house simply is not there yet.
    assert house_marks_as_of(account, old - dt.timedelta(days=1)) == {}


def test_marks_replace_rather_than_accumulate(house_account):
    """90 then 150 is a revaluation to 150, never a holding of 240."""
    user, account, house = house_account
    record_house_mark(
        user=user, account_id=account.id, asset=house, quantity=Decimal("90"),
        area_sqm=Decimal("90.2"),
        occurred_at=timezone.now() - dt.timedelta(days=400),
    )
    record_house_mark(
        user=user, account_id=account.id, asset=house, quantity=Decimal("150"),
        area_sqm=Decimal("90.2"),
        occurred_at=timezone.now() - dt.timedelta(days=10),
    )

    holding = Holding.objects.get(account=account, asset=house)

    assert holding.quantity == Decimal("150")
    assert holdings_as_of(user, account, timezone.now())["house_asset"] == Decimal("150")


def test_area_travels_with_the_mark_in_force(house_account):
    user, account, house = house_account
    old = timezone.now() - dt.timedelta(days=400)
    record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("90"), area_sqm=Decimal("90.2"), occurred_at=old,
    )
    record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("150"), area_sqm=Decimal("120.5"),
        occurred_at=timezone.now() - dt.timedelta(days=10),
    )

    # Pairing a historical price with today's area would mix two points in time.
    assert house_area_as_of(account, old)["house_asset"] == Decimal("90.2")
    assert house_area_as_of(account, timezone.now())["house_asset"] == Decimal("120.5")


def test_a_mark_cannot_be_dated_in_the_future(house_account):
    from portfolio.services.ledger import LedgerError

    user, account, house = house_account

    with pytest.raises(LedgerError):
        record_house_mark(
            user=user, account_id=account.id, asset=house,
            quantity=Decimal("90"), area_sqm=Decimal("90.2"),
            occurred_at=timezone.now() + dt.timedelta(days=1),
        )

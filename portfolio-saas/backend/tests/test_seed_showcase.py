"""The showcase seeder: builds through the app's own paths and rebuilds in place."""
from decimal import Decimal
from unittest import mock

import pytest
from django.core.management import CommandError, call_command

from portfolio.management.commands import seed_showcase
from portfolio.models import Account, Asset, LedgerEntry, Liability

pytestmark = pytest.mark.django_db

STORY = [
    (0, "family", "opening_cash", None, 1_000_000_000),
    (0, "family", "buy", ("key", "emami_coin"), 300_000_000),
    (6, "family", "sell", ("key", "emami_coin"), 0.5),
    (0, "savings", "opening_cash", None, 500_000_000),
    (0, "family", "buy", ("tse", "not-in-catalog"), 1),
]


@pytest.fixture
def story(asset_catalog):
    price = mock.patch("portfolio.services.ledger.resolve_historical_price", return_value=Decimal(100_000_000))
    mine = mock.patch.object(seed_showcase, "resolve_historical_price", return_value=Decimal(100_000_000))
    with price, mine, mock.patch.object(seed_showcase, "STORY", STORY):
        yield


def _built(user):
    accounts = Account.objects.filter(user=user)
    return (
        sorted(accounts.values_list("name", flat=True)),
        LedgerEntry.objects.filter(account__in=accounts).count(),
        Liability.objects.filter(account__in=accounts).count(),
        Asset.objects.filter(owner=user, is_house=True).count(),
    )


def test_rebuild_replaces_the_demo_in_place_and_keeps_the_password(story, make_user):
    user = make_user(email="demo@parhambm.ir", password="keep-this-one")
    hashed = user.password

    call_command("seed_showcase", "--reset", "--skip-research")
    first = _built(user)
    call_command("seed_showcase", "--reset", "--skip-research")

    # Three cash/trade rows + one sell + one savings opening, plus the apartment's
    # four dated marks; one mortgage on one minted apartment. Twice, same result.
    assert first == (["Family", "Savings"], 8, 1, 1)
    assert _built(user) == first
    user.refresh_from_db()
    assert user.password == hashed
    # The sell took half of the 3 coins the budget bought, rounded down to whole coins.
    sell = LedgerEntry.objects.get(account__user=user, kind="sell")
    assert sell.quantity == 1


def test_dry_run_keeps_nothing_and_an_existing_demo_needs_reset(story, make_user):
    call_command("seed_showcase", "--dry-run", "--skip-research")
    assert not Account.objects.exists()

    make_user(email="demo@parhambm.ir")
    with pytest.raises(CommandError, match="--reset"):
        call_command("seed_showcase", "--skip-research")

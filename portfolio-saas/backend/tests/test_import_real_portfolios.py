"""Integration tests for import_real_portfolios.

Rationale: the command spans User -> Account -> Holding -> Price -> Snapshot and
rewrites auto_now_add timestamps, and it must be idempotent; only a test against
the real ORM (fed a tiny two-day fixture) verifies rows land, dedupe per day,
value the house correctly, and re-run without duplicating.
"""
import json

import pytest
from django.core.management import call_command
from django.test import override_settings

from portfolio.models import Account, Holding, Price, Snapshot, Transaction
from portfolio.management.commands.import_real_portfolios import SAMPLE_EMAIL
from portfolio.services.valuation import value_account
from accounts.models import User

pytestmark = pytest.mark.django_db

# Mother holds the house (price/sqm-million = 90 -> 90*1e6*90.2 - 4e8).
STATE = {
    "Father": {"emami_coin": 21, "usd_cash": 3000},
    "Mother": {"gold_18k_gram": 165, "house_price_per_sqm_million": 90},
}

# Two calendar days, with an extra intraday snapshot on day 1 (must collapse to
# the latest one). Prices differ so the "latest of the day" choice is testable.
HISTORY = [
    {"timestamp": "2026-07-20T09:00:00",
     "snapshot": {"total_values_tomans": {"Father": 100, "Mother": 200},
                  "prices": {"emami_coin": 111, "usd_cash": 1}}},
    {"timestamp": "2026-07-20T18:00:00",  # later same day -> wins
     "snapshot": {"total_values_tomans": {"Father": 150, "Mother": 250},
                  "prices": {"emami_coin": 222, "usd_cash": 2}}},
    {"timestamp": "2026-07-21T18:00:00",
     "snapshot": {"total_values_tomans": {"Father": 160, "Mother": 260},
                  "prices": {"emami_coin": 333, "usd_cash": 3}}},
]


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "current_state.json").write_text(json.dumps(STATE))
    with (tmp_path / "history_snapshots.jsonl").open("w") as fh:
        for rec in HISTORY:
            fh.write(json.dumps(rec) + "\n")
    return tmp_path


@pytest.fixture
def imported(asset_catalog, data_dir):
    with override_settings(DEBUG=True):
        call_command("import_real_portfolios", "--data-dir", str(data_dir))
    return User.objects.get(email=SAMPLE_EMAIL)


def test_creates_named_accounts_and_exact_holdings(imported):
    accounts = {a.name: a for a in imported.accounts.all()}
    assert set(accounts) == {"Father", "Mother"}
    father = {h.asset.key: h.quantity for h in accounts["Father"].holdings.all()}
    assert father["emami_coin"] == 21 and father["usd_cash"] == 3000


def test_house_maps_to_house_asset_and_values_via_formula(imported):
    mother = imported.accounts.get(name="Mother")
    house = mother.holdings.get(asset__key="house_asset")
    assert house.quantity == 90
    # 90 * 1e6 * 90.2 - 4e8 = 7,718,000,000.
    val = {i["key"]: i["value"] for i in value_account(mother)["items"]}
    assert val["house_asset"] == 7_718_000_000


def test_collapses_to_one_snapshot_per_day(imported):
    user_snaps = Snapshot.objects.filter(user=imported, account__isnull=True)
    assert user_snaps.count() == 2  # two calendar days, intraday dupes collapsed
    # Latest-of-day wins: 2026-07-20 user total = 150 + 250 = 400 (not 300).
    day20 = user_snaps.order_by("timestamp").first()
    assert day20.total_value_tomans == 400


def test_prices_use_latest_of_day(imported):
    # emami_coin on 2026-07-20 should be 222 (the 18:00 row), not 111.
    prices = Price.objects.filter(source="REAL", asset__key="emami_coin").order_by("fetched_at")
    assert prices.count() == 2
    assert prices.first().price == 222


def test_idempotent_second_run_does_not_duplicate(imported, data_dir):
    holdings = Holding.objects.filter(account__user=imported).count()
    snaps = Snapshot.objects.filter(user=imported).count()
    prices = Price.objects.filter(source="REAL").count()
    with override_settings(DEBUG=True):
        call_command("import_real_portfolios", "--data-dir", str(data_dir))
    assert Holding.objects.filter(account__user=imported).count() == holdings
    assert Snapshot.objects.filter(user=imported).count() == snaps
    assert Price.objects.filter(source="REAL").count() == prices
    assert User.objects.filter(email=SAMPLE_EMAIL).count() == 1


def test_second_run_preserves_browser_changes(imported, data_dir, asset_catalog):
    account = imported.accounts.get(name="Father")
    Holding.objects.filter(account=account, asset=asset_catalog["emami_coin"]).update(quantity=99)
    Transaction.objects.create(
        account=account,
        asset=asset_catalog["emami_coin"],
        side=Transaction.Side.BUY,
        quantity=1,
    )
    with override_settings(DEBUG=True):
        call_command("import_real_portfolios", "--data-dir", str(data_dir))
    assert account.holdings.get(asset=asset_catalog["emami_coin"]).quantity == 99
    assert account.transactions.count() == 1


def test_skips_when_debug_off(asset_catalog, data_dir):
    with override_settings(DEBUG=False):
        call_command("import_real_portfolios", "--data-dir", str(data_dir))
    assert not User.objects.filter(email=SAMPLE_EMAIL).exists()

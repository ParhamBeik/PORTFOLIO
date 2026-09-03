"""The trust-first ledger: entries, trades, reversals, cost basis, and the invariants that keep a position and its history agreeing.

Merged from 4 files; each section keeps its original banner.
"""

import datetime
import datetime as dt
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone
import jdatetime
import pytest
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

import config.settings as settings_module
from marketdata.models import MarketCandle, GoldCurrencyHistory
from marketdata.models import MarketCandle, RejectedRecord
from portfolio.models import Account, Asset, Holding, Price, Transaction
from portfolio.models import Account, Holding, LedgerEntry
from portfolio.models import Account, Holding, LedgerEntry, Snapshot, Transaction
from portfolio.serializers import TradeInputSerializer
from portfolio.services.deflator import CpiUnavailable, cpi_for_date, to_basis
from portfolio.services.ledger import create_ledger_entry
from portfolio.services.returns import _price_version_fingerprint, daily_returns_matrix
from portfolio.services.timeline import holdings_as_of
from portfolio.services.trades import (
    InsufficientHolding,
    ManualAssetTrade,
    TradeError,
    execute_trade,
    undo_trade,
)
from portfolio.services.trades import execute_trade, undo_trade

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_ledger_api.py
# Account-ledger API integration tests.
# 
# Integration tests fit here because one request must keep the immutable ledger,
# holding projection, cash projection, ownership boundary, and reversal together.


@pytest.fixture
def ledger_account(asset_catalog, make_user):
    return Account.objects.create(user=make_user(email="ledger@test.test"), name="Ledger")


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _post(client, account, payload):
    return client.post(f"/api/accounts/{account.id}/ledger/", payload, format="json")


def test_opening_baseline_and_trade_update_cash_and_holdings(
    ledger_account, asset_catalog
):
    client = _client(ledger_account.user)
    started_at = (timezone.now() - datetime.timedelta(days=10)).isoformat()

    opening_cash = _post(client, ledger_account, {
        "kind": "opening_cash",
        "amount_tomans": "1000",
        "occurred_at": started_at,
    })
    opening_position = _post(client, ledger_account, {
        "kind": "opening_position",
        "asset_key": "emami_coin",
        "quantity": "2",
        "occurred_at": started_at,
    })
    buy = _post(client, ledger_account, {
        "kind": "buy",
        "asset_key": "emami_coin",
        "quantity": "1",
        "unit_price_tomans": "100",
        "occurred_at": (timezone.now() - datetime.timedelta(days=1)).isoformat(),
    })

    assert opening_cash.status_code == 201, opening_cash.data
    assert opening_position.status_code == 201, opening_position.data
    assert buy.status_code == 201, buy.data
    ledger_account.refresh_from_db()
    assert ledger_account.ledger_complete is True
    assert ledger_account.cash_balance_tomans == Decimal("900")
    assert Holding.objects.get(
        account=ledger_account, asset=asset_catalog["emami_coin"]
    ).quantity == Decimal("3")
    assert LedgerEntry.objects.filter(account=ledger_account).count() == 3


def test_buy_funds_itself_when_the_portfolio_does_not_track_cash(
    ledger_account, asset_catalog
):
    """A portfolio that has never recorded cash can still record a purchase.

    Booking every buy as a funded purchase meant this was refused with
    "Insufficient cash balance" -- the most common reason a trade could not be
    saved, and it looked like the app had lost the entry rather than declined it.
    The money is assumed to have come from outside the tracked portfolio.
    """
    client = _client(ledger_account.user)
    response = _post(client, ledger_account, {
        "kind": "buy",
        "asset_key": "emami_coin",
        "quantity": "1",
        "unit_price_tomans": "100",
        "occurred_at": timezone.now().isoformat(),
    })

    assert response.status_code == 201, response.data
    ledger_account.refresh_from_db()
    assert ledger_account.cash_balance_tomans == Decimal("0")
    assert ledger_account.track_cash is False
    assert Holding.objects.get(
        account=ledger_account, asset=asset_catalog["emami_coin"]
    ).quantity == Decimal("1")


def test_buy_cannot_make_cash_negative_once_cash_is_tracked(
    ledger_account, asset_catalog
):
    """Declaring cash opts the portfolio into settling trades against it."""
    client = _client(ledger_account.user)
    opened = _post(client, ledger_account, {
        "kind": "opening_cash",
        "amount_tomans": "50",
        "occurred_at": (timezone.now() - datetime.timedelta(days=1)).isoformat(),
    })
    assert opened.status_code == 201, opened.data

    response = _post(client, ledger_account, {
        "kind": "buy",
        "asset_key": "emami_coin",
        "quantity": "1",
        "unit_price_tomans": "100",
        "occurred_at": timezone.now().isoformat(),
    })

    assert response.status_code == 400
    assert response.data["detail"] == "Insufficient cash balance."
    ledger_account.refresh_from_db()
    assert ledger_account.track_cash is True
    assert ledger_account.cash_balance_tomans == Decimal("50")


def test_declaring_cash_does_not_retroactively_bill_earlier_trades(
    ledger_account, asset_catalog
):
    """Cash tracking starts where the cash history starts, not at the ledger's head.

    Applying it to the whole ledger at once meant recording your first deposit
    charged every past purchase against it and failed the replay for insufficient
    funds -- turning "I want to start tracking cash" into an unfixable error.
    """
    client = _client(ledger_account.user)
    bought = _post(client, ledger_account, {
        "kind": "buy",
        "asset_key": "emami_coin",
        "quantity": "1",
        "unit_price_tomans": "1000",
        "occurred_at": (timezone.now() - datetime.timedelta(days=5)).isoformat(),
    })
    assert bought.status_code == 201, bought.data

    deposited = _post(client, ledger_account, {
        "kind": "deposit",
        "amount_tomans": "300",
        "occurred_at": timezone.now().isoformat(),
    })

    assert deposited.status_code == 201, deposited.data
    ledger_account.refresh_from_db()
    # The earlier 1,000 purchase is not charged against the 300 deposit.
    assert ledger_account.cash_balance_tomans == Decimal("300")


def test_reversal_is_append_only_and_restores_projections(
    ledger_account, asset_catalog
):
    client = _client(ledger_account.user)
    occurred_at = (timezone.now() - datetime.timedelta(days=2)).isoformat()
    _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "500", "occurred_at": occurred_at,
    })
    created = _post(client, ledger_account, {
        "kind": "buy", "asset_key": "emami_coin", "quantity": "2",
        "unit_price_tomans": "100", "occurred_at": timezone.now().isoformat(),
    })

    response = client.post(
        f"/api/accounts/{ledger_account.id}/ledger/{created.data['id']}/reverse/",
        {},
        format="json",
    )

    assert response.status_code == 201, response.data
    ledger_account.refresh_from_db()
    assert ledger_account.cash_balance_tomans == Decimal("500")
    assert not Holding.objects.filter(
        account=ledger_account, asset=asset_catalog["emami_coin"]
    ).exists()
    assert LedgerEntry.objects.filter(account=ledger_account).count() == 3
    assert LedgerEntry.objects.get(pk=response.data["id"]).reversal_of_id == created.data["id"]


def test_ledger_is_account_scoped(ledger_account, make_user):
    response = _client(make_user(email="intruder-ledger@test.test")).get(
        f"/api/accounts/{ledger_account.id}/ledger/"
    )

    assert response.status_code == 404


def _csv_upload(content: str):
    return SimpleUploadedFile("ledger.csv", content.encode("utf-8"), "text/csv")


def test_csv_preview_validates_without_writing(ledger_account):
    occurred_at = (timezone.now() - datetime.timedelta(days=5)).isoformat()
    content = (
        "external_id,occurred_at,kind,asset_key,quantity,unit_price_tomans,amount_tomans,note\n"
        f"cash-1,{occurred_at},opening_cash,,,,1000,Starting cash\n"
    )

    response = _client(ledger_account.user).post(
        f"/api/accounts/{ledger_account.id}/imports/preview/",
        {"file": _csv_upload(content)},
        format="multipart",
    )

    assert response.status_code == 200, response.data
    assert response.data["valid"] is True
    assert response.data["row_count"] == 1
    assert not LedgerEntry.objects.filter(account=ledger_account).exists()


def test_csv_commit_is_atomic_and_idempotent(ledger_account, asset_catalog):
    occurred_at = (timezone.now() - datetime.timedelta(days=5)).isoformat()
    content = (
        "external_id,occurred_at,kind,asset_key,quantity,unit_price_tomans,amount_tomans,note\n"
        f"cash-1,{occurred_at},opening_cash,,,,1000,Starting cash\n"
        f"position-1,{occurred_at},opening_position,emami_coin,2,,,Starting position\n"
    )
    client = _client(ledger_account.user)

    first = client.post(
        f"/api/accounts/{ledger_account.id}/imports/commit/",
        {"file": _csv_upload(content)},
        format="multipart",
    )
    replay = client.post(
        f"/api/accounts/{ledger_account.id}/imports/commit/",
        {"file": _csv_upload(content)},
        format="multipart",
    )

    assert first.status_code == 201, first.data
    assert replay.status_code == 200, replay.data
    assert replay.data["batch_id"] == first.data["batch_id"]
    assert LedgerEntry.objects.filter(account=ledger_account).count() == 2


def test_csv_commit_rolls_back_every_row_on_error(ledger_account):
    occurred_at = (timezone.now() - datetime.timedelta(days=5)).isoformat()
    content = (
        "external_id,occurred_at,kind,asset_key,quantity,unit_price_tomans,amount_tomans,note\n"
        f"cash-1,{occurred_at},opening_cash,,,,1000,Starting cash\n"
        f"bad-1,{occurred_at},buy,unknown,2,100,,Bad asset\n"
    )

    response = _client(ledger_account.user).post(
        f"/api/accounts/{ledger_account.id}/imports/commit/",
        {"file": _csv_upload(content)},
        format="multipart",
    )

    assert response.status_code == 400
    assert response.data["row"] == 2
    assert not LedgerEntry.objects.filter(account=ledger_account).exists()


def test_account_performance_uses_only_external_cash_flows(
    ledger_account, asset_catalog, write_prices
):
    client = _client(ledger_account.user)
    # >= MIN_TRACKING_DAYS_FOR_ANNUALIZED (90): this test is about which flows
    # count as external, not about the insufficient-history gate.
    started_at = timezone.now() - datetime.timedelta(days=200)
    _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "1000",
        "occurred_at": started_at.isoformat(),
    })
    _post(client, ledger_account, {
        "kind": "buy", "asset_key": "emami_coin", "quantity": "2",
        "unit_price_tomans": "100",
        "occurred_at": (started_at + datetime.timedelta(days=1)).isoformat(),
    })
    write_prices({"emami_coin": Decimal("100")})

    response = client.get(
        f"/api/accounts/{ledger_account.id}/performance/?basis=nominal_toman"
    )

    assert response.status_code == 200, response.data
    assert response.data["performance_available"] is True
    assert response.data["external_flow_count"] == 0
    assert Decimal(response.data["current_value_tomans"]) == Decimal("1000")
    assert abs(response.data["twr"]) < 1e-9
    assert abs(response.data["xirr"]) < 1e-9


def test_account_performance_is_unavailable_without_complete_baseline(ledger_account):
    response = _client(ledger_account.user).get(
        f"/api/accounts/{ledger_account.id}/performance/"
    )

    assert response.status_code == 200
    assert response.data["performance_available"] is False
    # Machine-readable reason, so the UI can distinguish "no baseline yet" from
    # "not enough history yet" instead of parsing the prose.
    assert response.data["reason"] == "opening_baseline_missing"
    assert response.data["detail"] == (
        "Complete an opening baseline before calculating performance."
    )
    # Nothing numeric may be reported when performance is unavailable.
    assert "twr" not in response.data and "xirr" not in response.data


def test_legacy_performance_route_requires_and_scopes_account(ledger_account):
    client = _client(ledger_account.user)

    missing = client.get("/api/performance/")
    scoped = client.get(f"/api/performance/?account={ledger_account.id}")

    assert missing.status_code == 400
    assert missing.data["detail"] == "account query param is required."
    assert scoped.status_code == 200
    assert scoped.data["performance_available"] is False


def test_deposit_is_external_but_does_not_create_investment_return(ledger_account):
    client = _client(ledger_account.user)
    # >= MIN_TRACKING_DAYS_FOR_ANNUALIZED (90): this test is about which flows
    # count as external, not about the insufficient-history gate.
    started_at = timezone.now() - datetime.timedelta(days=200)
    _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "1000",
        "occurred_at": started_at.isoformat(),
    })
    _post(client, ledger_account, {
        "kind": "deposit", "amount_tomans": "500",
        "occurred_at": (started_at + datetime.timedelta(days=10)).isoformat(),
    })

    response = client.get(f"/api/accounts/{ledger_account.id}/performance/")

    assert response.status_code == 200
    assert response.data["external_flow_count"] == 1
    assert Decimal(response.data["current_value_tomans"]) == Decimal("1500")
    assert abs(response.data["twr"]) < 1e-9
    assert abs(response.data["xirr"]) < 1e-9


def test_dividend_requires_an_asset_but_not_a_quantity(ledger_account, asset_catalog):
    client = _client(ledger_account.user)
    _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "1000",
        "occurred_at": (timezone.now() - datetime.timedelta(days=1)).isoformat(),
    })

    response = _post(client, ledger_account, {
        "kind": "dividend", "asset_key": "emami_coin",
        "amount_tomans": "50", "occurred_at": timezone.now().isoformat(),
    })

    assert response.status_code == 201, response.data
    assert response.data["quantity"] is None


def test_external_id_is_unique_within_an_account(ledger_account):
    fields = {
        "account": ledger_account,
        "kind": LedgerEntry.Kind.OPENING_CASH,
        "amount_tomans": Decimal("100"),
        "external_id": "bank-42",
    }
    LedgerEntry.objects.create(**fields)

    with pytest.raises(IntegrityError), transaction.atomic():
        LedgerEntry.objects.create(**fields)


def test_dividend_asset_is_enforced_by_database(ledger_account):
    with pytest.raises(IntegrityError), transaction.atomic():
        LedgerEntry.objects.create(
            account=ledger_account,
            kind=LedgerEntry.Kind.DIVIDEND,
            amount_tomans=Decimal("50"),
        )


def test_past_sell_before_any_buy_is_rejected(ledger_account, asset_catalog):
    client = _client(ledger_account.user)
    now = timezone.now()
    assert _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "10000",
        "occurred_at": (now - datetime.timedelta(days=10)).isoformat(),
    }).status_code == 201
    assert _post(client, ledger_account, {
        "kind": "buy", "asset_key": "emami_coin", "quantity": "2",
        "unit_price_tomans": "100",
        "occurred_at": (now - datetime.timedelta(days=1)).isoformat(),
    }).status_code == 201

    response = _post(client, ledger_account, {
        "kind": "sell", "asset_key": "emami_coin", "quantity": "1",
        "unit_price_tomans": "120",
        "occurred_at": (now - datetime.timedelta(days=5)).isoformat(),
    })

    assert response.status_code == 400
    assert response.data["detail"] == "Insufficient holding quantity."
    assert LedgerEntry.objects.filter(account=ledger_account, kind="sell").count() == 0
    assert Holding.objects.get(
        account=ledger_account, asset=asset_catalog["emami_coin"]
    ).quantity == Decimal("2")


def test_past_sell_between_buy_and_later_sale_is_accepted(ledger_account, asset_catalog):
    client = _client(ledger_account.user)
    now = timezone.now()
    opened = now - datetime.timedelta(days=10)
    bought = now - datetime.timedelta(days=8)
    later_sale = now - datetime.timedelta(days=1)
    mid_sale = now - datetime.timedelta(days=4)
    assert _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "10000",
        "occurred_at": opened.isoformat(),
    }).status_code == 201
    assert _post(client, ledger_account, {
        "kind": "buy", "asset_key": "emami_coin", "quantity": "2",
        "unit_price_tomans": "100", "occurred_at": bought.isoformat(),
    }).status_code == 201
    assert _post(client, ledger_account, {
        "kind": "sell", "asset_key": "emami_coin", "quantity": "1",
        "unit_price_tomans": "130", "occurred_at": later_sale.isoformat(),
    }).status_code == 201

    response = _post(client, ledger_account, {
        "kind": "sell", "asset_key": "emami_coin", "quantity": "1",
        "unit_price_tomans": "120", "occurred_at": mid_sale.isoformat(),
    })

    assert response.status_code == 201, response.data
    ledger_account.refresh_from_db()
    assert not Holding.objects.filter(
        account=ledger_account, asset=asset_catalog["emami_coin"]
    ).exists()
    assert ledger_account.cash_balance_tomans == Decimal("10000") - 200 + 120 + 130


def test_ledger_list_reports_fifo_pnl(ledger_account, asset_catalog, write_prices):
    client = _client(ledger_account.user)
    now = timezone.now()
    assert _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "10000",
        "occurred_at": (now - datetime.timedelta(days=5)).isoformat(),
    }).status_code == 201
    buy = _post(client, ledger_account, {
        "kind": "buy", "asset_key": "emami_coin", "quantity": "2",
        "unit_price_tomans": "100",
        "occurred_at": (now - datetime.timedelta(days=4)).isoformat(),
    })
    assert buy.status_code == 201, buy.data
    write_prices({"emami_coin": Decimal("150")})

    listed = client.get(f"/api/accounts/{ledger_account.id}/ledger/")
    assert listed.status_code == 200
    buy_row = next(row for row in listed.data if row["id"] == buy.data["id"])
    assert buy_row["pnl_kind"] == "unrealized"
    assert Decimal(buy_row["pnl_tomans"]) == Decimal("100")

    sell = _post(client, ledger_account, {
        "kind": "sell", "asset_key": "emami_coin", "quantity": "1",
        "unit_price_tomans": "140",
        "occurred_at": (now - datetime.timedelta(days=1)).isoformat(),
    })
    assert sell.status_code == 201, sell.data
    listed = client.get(f"/api/accounts/{ledger_account.id}/ledger/")
    buy_row = next(row for row in listed.data if row["id"] == buy.data["id"])
    sell_row = next(row for row in listed.data if row["id"] == sell.data["id"])
    assert buy_row["pnl_kind"] == "unrealized"
    assert Decimal(buy_row["pnl_tomans"]) == Decimal("50")
    assert sell_row["pnl_kind"] == "realized"
    assert Decimal(sell_row["pnl_tomans"]) == Decimal("40")


def test_ledger_list_null_price_has_null_pnl(ledger_account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("150")})
    LedgerEntry.objects.create(
        account=ledger_account,
        asset=asset_catalog["emami_coin"],
        kind=LedgerEntry.Kind.BUY,
        quantity=Decimal("2"),
        price_tomans=None,
        amount_tomans=Decimal("0"),
        timestamp=timezone.now() - datetime.timedelta(days=2),
    )

    listed = _client(ledger_account.user).get(
        f"/api/accounts/{ledger_account.id}/ledger/"
    )

    assert listed.status_code == 200
    row = listed.data[0]
    assert row["pnl_tomans"] is None
    assert row["pnl_kind"] is None


def test_buy_omitted_price_uses_live_price(ledger_account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    client = _client(ledger_account.user)
    now = timezone.now()
    _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "1000000000000",
        "occurred_at": (now - datetime.timedelta(days=1)).isoformat(),
    })
    response = _post(client, ledger_account, {
        "kind": "buy", "asset_key": "emami_coin", "quantity": "1",
        "occurred_at": now.isoformat(),
    })

    assert response.status_code == 201, response.data
    assert Decimal(response.data["unit_price_tomans"]) == Decimal("176000000")


def test_patch_buy_quantity_rebuilds_holding(ledger_account, asset_catalog):
    client = _client(ledger_account.user)
    now = timezone.now()
    _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "10000",
        "occurred_at": (now - datetime.timedelta(days=2)).isoformat(),
    })
    buy = _post(client, ledger_account, {
        "kind": "buy", "asset_key": "emami_coin", "quantity": "2",
        "unit_price_tomans": "100",
        "occurred_at": now.isoformat(),
    })
    assert buy.status_code == 201, buy.data

    response = client.patch(
        f"/api/accounts/{ledger_account.id}/ledger/{buy.data['id']}/",
        {"quantity": "3"},
        format="json",
    )
    assert response.status_code == 200, response.data
    assert Decimal(response.data["quantity"]) == Decimal("3")
    assert Holding.objects.get(
        account=ledger_account, asset=asset_catalog["emami_coin"]
    ).quantity == Decimal("3")


def test_delete_buy_removes_holding(ledger_account, asset_catalog):
    client = _client(ledger_account.user)
    now = timezone.now()
    _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "10000",
        "occurred_at": (now - datetime.timedelta(days=2)).isoformat(),
    })
    buy = _post(client, ledger_account, {
        "kind": "buy", "asset_key": "emami_coin", "quantity": "2",
        "unit_price_tomans": "100",
        "occurred_at": now.isoformat(),
    })
    response = client.delete(
        f"/api/accounts/{ledger_account.id}/ledger/{buy.data['id']}/"
    )
    assert response.status_code == 204
    assert not LedgerEntry.objects.filter(pk=buy.data["id"]).exists()
    assert not Holding.objects.filter(
        account=ledger_account, asset=asset_catalog["emami_coin"]
    ).exists()


def test_edit_sell_qty_above_holdings_is_rejected(ledger_account, asset_catalog):
    client = _client(ledger_account.user)
    now = timezone.now()
    _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "10000",
        "occurred_at": (now - datetime.timedelta(days=3)).isoformat(),
    })
    _post(client, ledger_account, {
        "kind": "buy", "asset_key": "emami_coin", "quantity": "2",
        "unit_price_tomans": "100",
        "occurred_at": (now - datetime.timedelta(days=2)).isoformat(),
    })
    sell = _post(client, ledger_account, {
        "kind": "sell", "asset_key": "emami_coin", "quantity": "1",
        "unit_price_tomans": "120",
        "occurred_at": now.isoformat(),
    })
    response = client.patch(
        f"/api/accounts/{ledger_account.id}/ledger/{sell.data['id']}/",
        {"quantity": "5"},
        format="json",
    )
    assert response.status_code == 400
    assert Holding.objects.get(
        account=ledger_account, asset=asset_catalog["emami_coin"]
    ).quantity == Decimal("1")


def test_all_portfolios_ledger_lists_every_account(ledger_account, asset_catalog, make_user):
    other = Account.objects.create(user=ledger_account.user, name="Other")
    now = timezone.now()
    client = _client(ledger_account.user)
    _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "1000",
        "occurred_at": now.isoformat(),
    })
    _post(client, other, {
        "kind": "opening_cash", "amount_tomans": "2000",
        "occurred_at": now.isoformat(),
    })
    response = client.get("/api/ledger/")
    assert response.status_code == 200
    names = {row["account_name"] for row in response.data}
    assert "Ledger" in names
    assert "Other" in names


def test_holding_without_ledger_appears_as_position(ledger_account, asset_catalog):
    Holding.objects.create(
        account=ledger_account,
        asset=asset_catalog["emami_coin"],
        quantity=Decimal("4"),
    )
    response = _client(ledger_account.user).get(
        f"/api/accounts/{ledger_account.id}/ledger/"
    )
    assert response.status_code == 200
    positions = [row for row in response.data if row.get("is_synthetic")]
    assert len(positions) == 1
    assert positions[0]["kind"] == "position"
    assert Decimal(positions[0]["quantity"]) == Decimal("4")
    assert positions[0]["asset_key"] == "emami_coin"


# ----------------------------------------------------------------------
# test_trades.py
# Trade execution ledger — integration tests.
# 
# Integration type: `execute_trade` spans the ledger, Holding, cash, and Snapshot
# inside one atomic transaction. Buys require funded cash; undos append reversals.


# Large enough for every buy quantity x price used in this module.
_FUND = Decimal("100000000000000")


@pytest.fixture
def account(asset_catalog, make_user):
    user = make_user(email="trader@test.test")
    acc = Account.objects.create(user=user, name="Main")
    create_ledger_entry(
        account=acc,
        kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans=_FUND,
        occurred_at=timezone.now() - datetime.timedelta(days=400),
        note="Test funding",
    )
    return acc


def test_buy_creates_holding_ledger_and_snapshot(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    result = execute_trade(
        account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("3")
    )

    holding = Holding.objects.get(account=account, asset=asset_catalog["emami_coin"])
    assert holding.quantity == Decimal("3")
    txn = Transaction.objects.filter(account=account, kind="buy").get()
    assert txn.quantity == Decimal("3")
    assert txn.price_tomans == Decimal("176000000.0000")
    assert Snapshot.objects.filter(user=account.user, account=None).count() == 1
    assert result["holding_quantity"] == "3"
    assert Decimal(result["holding_quantity"]) == Decimal("3")
    account.refresh_from_db()
    assert account.cash_balance_tomans == _FUND - (Decimal("176000000") * 3)


def test_buy_accumulates_into_existing_holding(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("2"))
    execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("1.5"))
    holding = Holding.objects.get(account=account, asset=asset_catalog["emami_coin"])
    assert holding.quantity == Decimal("3.5")
    assert Transaction.objects.filter(account=account, kind="buy").count() == 2


def test_sell_reduces_holding(account, asset_catalog, write_prices):
    write_prices({"kama_stock": Decimal("1780")})
    execute_trade(account=account, asset=asset_catalog["kama_stock"], side="buy", quantity=Decimal("100000"))
    execute_trade(account=account, asset=asset_catalog["kama_stock"], side="sell", quantity=Decimal("40000"))
    holding = Holding.objects.get(account=account, asset=asset_catalog["kama_stock"])
    assert holding.quantity == Decimal("60000")


def test_full_sell_removes_holding_but_keeps_ledger(account, asset_catalog, write_prices):
    write_prices({"kama_stock": Decimal("1780")})
    execute_trade(account=account, asset=asset_catalog["kama_stock"], side="buy", quantity=Decimal("100"))
    execute_trade(account=account, asset=asset_catalog["kama_stock"], side="sell", quantity=Decimal("100"))
    assert not Holding.objects.filter(account=account, asset=asset_catalog["kama_stock"]).exists()
    assert Transaction.objects.filter(account=account, kind__in=["buy", "sell"]).count() == 2


def test_oversell_is_rejected_and_atomic(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("2"))
    with pytest.raises(InsufficientHolding):
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="sell", quantity=Decimal("5"))
    assert Transaction.objects.filter(account=account, kind="buy").count() == 1
    assert Holding.objects.get(account=account, asset=asset_catalog["emami_coin"]).quantity == Decimal("2")


def test_sell_with_no_holding_is_rejected(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    with pytest.raises(InsufficientHolding):
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="sell", quantity=Decimal("1"))


def test_house_asset_is_not_tradeable(account, asset_catalog):
    with pytest.raises(ManualAssetTrade):
        execute_trade(account=account, asset=asset_catalog["house_asset"], side="buy", quantity=Decimal("50"))


def test_non_positive_quantity_is_rejected(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    with pytest.raises(TradeError):
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("0"))


def test_bad_side_is_rejected(account, asset_catalog):
    with pytest.raises(TradeError):
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="hold", quantity=Decimal("1"))


def test_buy_without_price_is_rejected_atomically(account, asset_catalog):
    with pytest.raises(TradeError, match="No valid execution price"):
        execute_trade(
            account=account,
            asset=asset_catalog["one_gram_coin"],
            side="buy",
            quantity=Decimal("4"),
        )
    assert not Transaction.objects.filter(account=account, kind="buy").exists()
    assert not Holding.objects.filter(account=account).exists()


def test_undo_latest_trade_reverses_holding_and_stamps_snapshots(
    account, asset_catalog, write_prices
):
    write_prices({"emami_coin": Decimal("176000000")})
    execute_trade(
        account=account,
        asset=asset_catalog["emami_coin"],
        side="buy",
        quantity=Decimal("3"),
    )
    trade = Transaction.objects.filter(account=account, kind="buy").get()

    undo_trade(user=account.user, transaction_id=trade.id)

    # Append-only: original row remains; a system reversal nets it out.
    assert Transaction.objects.filter(pk=trade.id).exists()
    assert Transaction.objects.filter(account=account, reversal_of=trade).exists()
    assert not Holding.objects.filter(
        account=account, asset=asset_catalog["emami_coin"]
    ).exists()
    assert Snapshot.objects.filter(user=account.user, account=None).count() == 2
    assert Snapshot.objects.filter(user=account.user, account=account).count() == 2


def test_undo_rejects_when_reversal_would_go_negative(
    account, asset_catalog, write_prices
):
    write_prices({"emami_coin": Decimal("176000000")})
    execute_trade(
        account=account,
        asset=asset_catalog["emami_coin"],
        side="buy",
        quantity=Decimal("3"),
    )
    first = Transaction.objects.filter(account=account, kind="buy").get()
    execute_trade(
        account=account,
        asset=asset_catalog["emami_coin"],
        side="sell",
        quantity=Decimal("1"),
    )

    with pytest.raises(TradeError):
        undo_trade(user=account.user, transaction_id=first.id)

    assert Transaction.objects.filter(account=account, kind__in=["buy", "sell"]).count() == 2
    assert Holding.objects.get(
        account=account, asset=asset_catalog["emami_coin"]
    ).quantity == Decimal("2")


class TestTradeEndpoint:
    """The HTTP surface: auth, ownership, and error mapping."""

    def _client(self, user):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_post_trade_creates_transaction(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        client = self._client(account.user)
        resp = client.post(
            f"/api/accounts/{account.id}/trades/",
            {"asset_key": "emami_coin", "side": "buy", "quantity": "2"},
            format="json",
        )
        assert resp.status_code == 201
        assert Decimal(resp.data["holding_quantity"]) == Decimal("2")
        account.refresh_from_db()
        assert account.cash_balance_tomans == _FUND - (Decimal("176000000") * 2)

    def test_post_trade_preserves_historical_fields(self, account, asset_catalog):
        occurred_at = timezone.now() - datetime.timedelta(days=30)
        client = self._client(account.user)

        response = client.post(
            f"/api/accounts/{account.id}/trades/",
            {
                "asset_key": "emami_coin",
                "side": "buy",
                "quantity": "2",
                "timestamp": occurred_at.isoformat(),
                "price_tomans": "123456.75",
                "source": "imported",
            },
            format="json",
        )

        assert response.status_code == 201, response.data
        entry = Transaction.objects.filter(account=account, kind="buy").get()
        assert entry.timestamp == occurred_at
        assert entry.price_tomans == Decimal("123456.7500")
        # Compatibility map: imported → csv on the ledger.
        assert entry.source == "csv"

    def test_oversell_returns_400(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        client = self._client(account.user)
        resp = client.post(
            f"/api/accounts/{account.id}/trades/",
            {"asset_key": "emami_coin", "side": "sell", "quantity": "1"},
            format="json",
        )
        assert resp.status_code == 400

    def test_missing_execution_price_returns_400(self, account, asset_catalog):
        response = self._client(account.user).post(
            f"/api/accounts/{account.id}/trades/",
            {"asset_key": "one_gram_coin", "side": "buy", "quantity": "1"},
            format="json",
        )

        assert response.status_code == 400
        body = response.data
        assert "price_tomans" in body or "detail" in body
        assert not Transaction.objects.filter(account=account, kind="buy").exists()

    def test_cannot_trade_in_another_users_account(self, account, asset_catalog, make_user):
        other = make_user(email="intruder@test.test")
        client = self._client(other)
        resp = client.post(
            f"/api/accounts/{account.id}/trades/",
            {"asset_key": "emami_coin", "side": "buy", "quantity": "1"},
            format="json",
        )
        assert resp.status_code == 404

    def test_snapshot_endpoint_returns_series_and_trades(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("2"))
        client = self._client(account.user)
        resp = client.get("/api/snapshots/")
        assert resp.status_code == 200
        assert "series" in resp.data and "trades" in resp.data
        assert len(resp.data["trades"]) == 1
        assert resp.data["trades"][0]["asset_key"] == "emami_coin"

    def test_dashboard_can_add_tradeable_holding_through_ledger(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        client = self._client(account.user)
        resp = client.post(
            f"/api/accounts/{account.id}/holdings/",
            {"asset_key": "emami_coin", "quantity": "2"},
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert account.holdings.get(asset__key="emami_coin").quantity == Decimal("2")
        assert LedgerEntry.objects.filter(
            account=account, asset__key="emami_coin", kind=LedgerEntry.Kind.BUY
        ).exists()

    def test_dashboard_add_needs_no_cash_on_a_holdings_only_account(
        self, asset_catalog, make_user, write_prices
    ):
        """An account that never opened a cash ledger can still book a purchase.

        It used to record a bare position instead, because a funded purchase
        would have been rejected for insufficient funds. Now that a buy funds
        itself, the acquisition is the dated, priced event it really is -- which
        is also the only way the holding gets a cost basis.
        """
        write_prices({"emami_coin": Decimal("176000000")})
        cashless = Account.objects.create(
            user=make_user(email="holdings-only@test.test"), name="Holdings only"
        )
        resp = self._client(cashless.user).post(
            f"/api/accounts/{cashless.id}/holdings/",
            {"asset_key": "emami_coin", "quantity": "2"},
            format="json",
        )
        assert resp.status_code == 201, resp.data
        assert cashless.holdings.get(asset__key="emami_coin").quantity == Decimal("2")
        assert LedgerEntry.objects.filter(
            account=cashless, kind=LedgerEntry.Kind.BUY
        ).exists()
        cashless.refresh_from_db()
        assert cashless.cash_balance_tomans == Decimal("0")

    def test_holding_detail_rejects_wrong_parent_account(self, account, asset_catalog):
        other = Account.objects.create(user=account.user, name="Other")
        holding = Holding.objects.create(
            account=account,
            asset=asset_catalog["house_asset"],
            quantity=Decimal("50"),
        )
        response = self._client(account.user).patch(
            f"/api/accounts/{other.id}/holdings/{holding.id}/",
            {"quantity": "55"},
            format="json",
        )
        assert response.status_code == 404
        holding.refresh_from_db()
        assert holding.quantity == Decimal("50")

    def test_manual_holding_patch_updates_quantity_and_price(self, account, asset_catalog, write_prices):
        write_prices({"swiss_gold_bar_1g": Decimal("5000000")})
        holding = Holding.objects.create(
            account=account,
            asset=asset_catalog["swiss_gold_bar_1g"],
            quantity=Decimal("2"),
        )
        response = self._client(account.user).patch(
            f"/api/accounts/{account.id}/holdings/{holding.id}/",
            {"quantity": "3", "unit_price_tomans": "5500000"},
            format="json",
        )
        assert response.status_code == 200, response.data
        holding.refresh_from_db()
        assert holding.quantity == Decimal("3")
        from portfolio.models import Price

        latest = Price.objects.filter(asset=asset_catalog["swiss_gold_bar_1g"]).order_by("-id").first()
        assert latest.price == Decimal("5500000")
        assert latest.source == "manual"

    def test_dashboard_can_correct_tradeable_holding_quantity(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("1"))
        holding = Holding.objects.get(account=account, asset=asset_catalog["emami_coin"])
        response = self._client(account.user).patch(
            f"/api/accounts/{account.id}/holdings/{holding.id}/",
            {"quantity": "2"},
            format="json",
        )
        assert response.status_code == 200, response.data
        holding.refresh_from_db()
        assert holding.quantity == Decimal("2")
        assert LedgerEntry.objects.filter(
            account=account, asset=asset_catalog["emami_coin"], kind=LedgerEntry.Kind.BUY
        ).count() == 2

    def test_dashboard_can_downsize_holding_to_zero(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("1"))
        holding = Holding.objects.get(account=account, asset=asset_catalog["emami_coin"])
        response = self._client(account.user).patch(
            f"/api/accounts/{account.id}/holdings/{holding.id}/",
            {"quantity": "0"},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert not Holding.objects.filter(pk=holding.id).exists()

    def test_house_holding_rejects_nonpositive_price(self, account, asset_catalog):
        response = self._client(account.user).post(
            f"/api/accounts/{account.id}/holdings/",
            {"asset_key": "house_asset", "quantity": 0},
            format="json",
        )

        assert response.status_code == 400
        assert not account.holdings.filter(asset__key="house_asset").exists()

    def test_duplicate_house_holding_returns_400(self, account, asset_catalog):
        Holding.objects.create(
            account=account,
            asset=asset_catalog["house_asset"],
            quantity=Decimal("50"),
        )
        response = self._client(account.user).post(
            f"/api/accounts/{account.id}/holdings/",
            {"asset_key": "house_asset", "quantity": "55"},
            format="json",
        )
        assert response.status_code == 400
        assert account.holdings.filter(asset=asset_catalog["house_asset"]).count() == 1

    def test_cannot_undo_another_users_trade(
        self, account, asset_catalog, write_prices, make_user
    ):
        write_prices({"emami_coin": Decimal("176000000")})
        execute_trade(
            account=account,
            asset=asset_catalog["emami_coin"],
            side="buy",
            quantity=Decimal("2"),
        )
        trade = Transaction.objects.filter(account=account, kind="buy").get()

        response = self._client(make_user(email="other-trader@test.test")).delete(
            f"/api/transactions/{trade.id}/"
        )

        assert response.status_code == 404
        assert Transaction.objects.filter(pk=trade.id).exists()


def test_execute_trade_with_custom_price(account, asset_catalog):
    """Verify execute_trade respects custom passed prices."""
    result = execute_trade(
        account=account,
        asset=asset_catalog["emami_coin"],
        side="buy",
        quantity=Decimal("5"),
        price_tomans=Decimal("123456.789"),
    )
    holding = Holding.objects.get(account=account, asset=asset_catalog["emami_coin"])
    assert holding.quantity == Decimal("5")
    txn = Transaction.objects.filter(account=account, kind="buy").get()
    assert txn.quantity == Decimal("5")
    assert txn.price_tomans == Decimal("123456.7890")


def test_past_sell_before_buy_is_rejected(account, asset_catalog):
    now = timezone.now()
    execute_trade(
        account=account,
        asset=asset_catalog["emami_coin"],
        side="buy",
        quantity=Decimal("2"),
        price_tomans=Decimal("100"),
        timestamp=now - datetime.timedelta(days=1),
    )
    with pytest.raises(InsufficientHolding):
        execute_trade(
            account=account,
            asset=asset_catalog["emami_coin"],
            side="sell",
            quantity=Decimal("1"),
            price_tomans=Decimal("120"),
            timestamp=now - datetime.timedelta(days=5),
        )
    assert Transaction.objects.filter(account=account, kind="sell").count() == 0
    assert Holding.objects.get(
        account=account, asset=asset_catalog["emami_coin"]
    ).quantity == Decimal("2")


def test_backfill_ledger_gap_command(account, asset_catalog, write_prices):
    """Orphan holdings become opening_position rows, not invented buys."""
    Holding.objects.create(
        account=account,
        asset=asset_catalog["emami_coin"],
        quantity=Decimal("12.5"),
    )
    write_prices({"emami_coin": Decimal("20000000")})

    assert not Transaction.objects.filter(
        account=account, asset=asset_catalog["emami_coin"]
    ).exists()

    from django.core.management import call_command

    call_command("backfill_ledger_gap", "--price-source=zero")
    assert not Transaction.objects.filter(
        account=account, asset=asset_catalog["emami_coin"]
    ).exists()

    call_command("backfill_ledger_gap", "--price-source=latest", "--commit")
    txn = Transaction.objects.get(account=account, asset=asset_catalog["emami_coin"])
    assert txn.kind == "opening_position"
    assert txn.quantity == Decimal("12.5")

    Holding.objects.create(
        account=account,
        asset=asset_catalog["kama_stock"],
        quantity=Decimal("100"),
    )
    call_command("backfill_ledger_gap", "--price-source=manual", "--price=55.5", "--commit")
    txn2 = Transaction.objects.get(account=account, asset=asset_catalog["kama_stock"])
    assert txn2.kind == "opening_position"
    assert txn2.quantity == Decimal("100")


@pytest.mark.django_db
def test_holdings_list_query_count_is_flat_in_holding_count(
    asset_catalog, make_user
):
    """Listing holdings must not cost a query per holding.

    HoldingSerializer reads five columns off `asset` plus `obj.label`, and
    Django resolves a forward FK lazily per instance, so without the join the
    endpoint costs one extra round trip for every row -- on the request the
    dashboard makes first and most often. Comparing two portfolios rather than
    asserting an absolute number keeps this pinned to the growth rate, which is
    the actual invariant; the fixed prelude (auth, account lookup) is free to
    change.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    user = make_user(email="nplusone@test.test")
    one = Account.objects.create(user=user, name="One")
    many = Account.objects.create(user=user, name="Many")
    keys = list(asset_catalog)[:6]
    assert len(keys) >= 4, "fixture no longer has enough assets to see the slope"

    Holding.objects.create(
        account=one, asset=asset_catalog[keys[0]], quantity=Decimal("1")
    )
    for key in keys:
        Holding.objects.create(
            account=many, asset=asset_catalog[key], quantity=Decimal("1")
        )

    client = _client(user)
    with CaptureQueriesContext(connection) as few:
        assert client.get(f"/api/accounts/{one.id}/holdings/").status_code == 200
    with CaptureQueriesContext(connection) as lots:
        resp = client.get(f"/api/accounts/{many.id}/holdings/")
        assert resp.status_code == 200
    assert len(resp.data) == len(keys)

    assert len(lots) == len(few), (
        f"query count grew with the holding count ({len(few)} -> {len(lots)}) "
        f"across {len(keys) - 1} extra rows: the asset join was dropped"
    )


# ----------------------------------------------------------------------
# test_basis_integrity.py
# Valuation-basis integrity: real_toman must actually deflate, and CPI gaps
# must fail loud rather than silently clamp or masquerade as nominal data.
# 
# Unit tests throughout — pure logic in returns.py/deflator.py/settings.py
# around a few seeded warehouse rows, no HTTP/view layer involved.


def _seed_warehouse_days(symbol: str, n: int, end: dt.date, price: float = 8000.0):
    """n consecutive daily closes at a CONSTANT price, ending on `end` (Gregorian)."""
    rows = []
    for i in range(n):
        day = end - dt.timedelta(days=n - 1 - i)
        jday = jdatetime.date.fromgregorian(date=day)
        date_str = f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}"
        rows.append(MarketCandle(
            symbol=symbol,
            timeframe="1d_adj",
            date_time=date_str,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=1000,
        ))
    MarketCandle.objects.bulk_create(rows, ignore_conflicts=True)


@pytest.fixture(autouse=True)
def _known_cpi_table(monkeypatch):
    """Deterministic CPI table regardless of ambient CPI_BY_JALALI_YEAR_EXTRA."""
    monkeypatch.setattr(settings_module, "CPI_BY_JALALI_YEAR", {
        1398: 100.0,
        1399: 136.4,
        1400: 191.2,
        1401: 278.8,
        1402: 392.3,
        1403: 519.8,
        1404: 680.9,
    })


def test_real_toman_diverges_from_nominal_across_a_cpi_change(asset_catalog):
    """Bug 1 regression guard: a flat nominal price must NOT read flat in real_toman.

    Window is anchored (as_of) inside Jalali 1403, spanning back across the
    1402->1403 CPI change (392.3 -> 519.8), with a constant nominal price. If
    real_toman were silently falling through to the nominal matrix (the bug),
    the two would be numerically identical.
    """
    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    as_of = dt.datetime(2024, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)  # Jalali ~1403-03-12
    _seed_warehouse_days("کاما", 220, end=as_of.date())

    nominal_df, _ = daily_returns_matrix(
        as_of=as_of, history_days=200, universe=["kama_stock"], basis="nominal_toman"
    )
    real_df, _ = daily_returns_matrix(
        as_of=as_of, history_days=200, universe=["kama_stock"], basis="real_toman"
    )

    assert "kama_stock" in nominal_df.columns
    assert "kama_stock" in real_df.columns
    nominal = nominal_df["kama_stock"].dropna()
    real = real_df["kama_stock"].dropna()
    assert len(nominal) > 0 and len(real) > 0

    # Flat nominal price -> ~0 nominal daily return every day.
    assert nominal.abs().max() < 1e-9
    # CPI rises every day (even within a year, via linear interpolation), so
    # the same flat nominal price deflates -> strictly negative real return.
    assert real.abs().max() > 1e-9
    assert (real < 0).all()
    assert not nominal.equals(real)


def test_cpi_for_beyond_table_raises_not_clamps():
    """Bug 2: a year past the table is a loud, typed failure, not a clamp to the last value."""
    with pytest.raises(CpiUnavailable) as exc_info:
        settings_module.cpi_for(1405)
    assert exc_info.value.jalali_year == 1405
    assert exc_info.value.last_verified_year == 1404


def test_cpi_for_date_within_known_year_still_works():
    # 2024-06-01 falls in Jalali 1403 (which starts 2024-03-20), interpolating
    # toward the known 1404 value -- must resolve without raising.
    value = cpi_for_date("2024-06-01")  # Jalali ~1403-03-12
    assert 519.8 < value < 680.9


def test_real_toman_unavailable_when_cpi_unknown_does_not_fall_back_to_nominal(asset_catalog):
    """Bug 2: with no CPI for the current Jalali year, real_toman must fail loud,
    never silently return the nominal numbers under the real_toman label."""
    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])
    _seed_warehouse_days("کاما", 60, end=dt.date.today())

    # No as_of -> "now", whose Jalali year (1405) is deliberately absent from
    # the patched table above.
    with pytest.raises(CpiUnavailable):
        daily_returns_matrix(universe=["kama_stock"], basis="real_toman")

    # And the nominal basis must still work -- only real_toman is unavailable.
    nominal_df, _ = daily_returns_matrix(universe=["kama_stock"], basis="nominal_toman")
    assert "kama_stock" in nominal_df.columns


def test_cpi_override_extends_the_table(monkeypatch):
    """The override mechanism (CPI_BY_JALALI_YEAR_EXTRA merges into this exact
    dict at settings load) is picked up immediately by cpi_for."""
    with pytest.raises(CpiUnavailable):
        settings_module.cpi_for(1405)

    monkeypatch.setitem(settings_module.CPI_BY_JALALI_YEAR, 1405, 950.0)

    assert settings_module.cpi_for(1405) == 950.0


def test_fingerprint_rotates_when_rejected_record_added():
    """Bug 4: a new spike rejection must rotate the returns cache key immediately,
    not wait out the 600s TTL."""
    before = _price_version_fingerprint()
    RejectedRecord.objects.create(
        endpoint="stock_candle_adjusted", symbol="کاما", date="1403-01-01", reason="price_spike"
    )
    after = _price_version_fingerprint()
    assert before != after


# ----------------------------------------------------------------------
# test_track_a.py


@pytest.mark.django_db
class TestTrackA:
    def test_reconcile_ledger_clean(self, django_user_model):
        """Unit test for reconcile_ledger command when ledger is clean."""
        from portfolio.services.ledger import create_ledger_entry
        from portfolio.models import LedgerEntry

        user = django_user_model.objects.create_user(email="test@example.com", password="password123")
        acc = Account.objects.create(user=user, name="Main")
        asset = Asset.objects.create(key="test_asset", name="Test", is_active=True)
        create_ledger_entry(
            account=acc, kind=LedgerEntry.Kind.OPENING_CASH, amount_tomans="100000",
        )
        execute_trade(account=acc, asset=asset, side="buy", quantity="10.0", price_tomans="1000")

        call_command("reconcile_ledger")

    def test_reconcile_ledger_drift(self, django_user_model):
        """Unit test for reconcile_ledger command when ledger drifts."""
        from portfolio.services.ledger import create_ledger_entry
        from portfolio.models import LedgerEntry

        user = django_user_model.objects.create_user(email="test2@example.com", password="password123")
        acc = Account.objects.create(user=user, name="Main")
        asset = Asset.objects.create(key="test_asset", name="Test", is_active=True)
        create_ledger_entry(
            account=acc, kind=LedgerEntry.Kind.OPENING_CASH, amount_tomans="100000",
        )
        execute_trade(account=acc, asset=asset, side="buy", quantity="10.0", price_tomans="1000")

        h = Holding.objects.get(account=acc, asset=asset)
        h.quantity = Decimal("12.0")
        h.save()

        with pytest.raises(SystemExit):
            call_command("reconcile_ledger")

    def test_reconcile_ledger_fix_rebuilds_cash_and_holdings(self, django_user_model):
        from portfolio.services.ledger import create_ledger_entry
        from portfolio.models import LedgerEntry

        user = django_user_model.objects.create_user(email="fix@example.com", password="password123")
        acc = Account.objects.create(user=user, name="Main")
        asset = Asset.objects.create(key="fix_asset", name="Fix", is_active=True)
        create_ledger_entry(
            account=acc, kind=LedgerEntry.Kind.OPENING_CASH, amount_tomans="50000",
        )
        execute_trade(account=acc, asset=asset, side="buy", quantity="5", price_tomans="1000")
        acc.cash_balance_tomans = Decimal("1")
        acc.save(update_fields=["cash_balance_tomans"])
        Holding.objects.filter(account=acc).update(quantity=Decimal("99"))

        call_command("reconcile_ledger", "--fix")
        acc.refresh_from_db()
        assert acc.cash_balance_tomans == Decimal("45000")
        assert Holding.objects.get(account=acc, asset=asset).quantity == Decimal("5")

    def test_holdings_as_of(self, django_user_model):
        """Integration test for holdings_as_of backwards calculation."""
        user = django_user_model.objects.create_user(email="test3@example.com", password="password123")
        acc = Account.objects.create(user=user, name="Main")
        asset = Asset.objects.create(key="test_asset", name="Test", is_active=True)
        
        t1 = timezone.now() - datetime.timedelta(days=2)
        t2 = timezone.now() - datetime.timedelta(days=1)
        
        # Create transactions manually for precise timestamps
        Transaction.objects.create(account=acc, asset=asset, side="buy", quantity=Decimal("10"), price_tomans=1000, timestamp=t1)
        Transaction.objects.create(account=acc, asset=asset, side="sell", quantity=Decimal("4"), price_tomans=1100, timestamp=t2)
        Holding.objects.create(account=acc, asset=asset, quantity=Decimal("6"))
        
        # as of day 0 (now) -> 6
        assert holdings_as_of(user, acc, timezone.now()) == {"test_asset": Decimal("6.0")}
        
        # as of day -1.5 -> 10
        mid_date = timezone.now() - datetime.timedelta(days=1, hours=12)
        assert holdings_as_of(user, acc, mid_date) == {"test_asset": Decimal("10.0")}
        
        # as of day -3 -> 0
        old_date = timezone.now() - datetime.timedelta(days=3)
        assert holdings_as_of(user, acc, old_date) == {}
        
    def test_serializer_validation_future_date(self):
        """Unit test for rejecting future dates."""
        asset = Asset.objects.create(key="test", name="Test", is_active=True, asset_class="Stock", tse_symbol="FOO")
        
        data = {
            "asset_key": "test",
            "side": "buy",
            "quantity": "1.0",
            "timestamp": (timezone.now() + datetime.timedelta(days=1)).isoformat()
        }
        serializer = TradeInputSerializer(data=data)
        assert not serializer.is_valid()
        assert "timestamp" in serializer.errors

    def test_serializer_validation_before_history(self):
        """Unit test for rejecting dates before history."""
        asset = Asset.objects.create(key="test", name="Test", is_active=True, asset_class="Stock", tse_symbol="FOO")
        
        j_date_str = "1403-01-01"
        MarketCandle.objects.create(symbol="FOO", timeframe="1d_unadj", date_time=j_date_str + " 00:00:00", close_price=100)
        
        # Try a date before history (1399)
        dt = jdatetime.date(1399, 1, 1).togregorian()
        data = {
            "asset_key": "test",
            "side": "buy",
            "quantity": "1.0",
            "timestamp": datetime.datetime(dt.year, dt.month, dt.day, tzinfo=datetime.timezone.utc).isoformat()
        }
        
        serializer = TradeInputSerializer(data=data)
        assert not serializer.is_valid()
        assert "timestamp" in serializer.errors


def test_resolve_historical_price_falls_back_to_the_daily_bar():
    """Unit test: crypto/commodity/ETF/index/derivative assets have no provider
    history endpoint, so the distilled daily bar is the only close they ever
    get -- without it an opening trade on one of them cannot be priced at all."""
    from marketdata.models import MarketDailyBar, MarketInstrument
    from portfolio.services.ledger import resolve_historical_price

    asset = Asset.objects.create(
        key="leverage_etf", name="Leverage ETF", is_active=True,
        asset_class=Asset.AssetClass.STOCK, tse_symbol="اهرم",
    )
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC,
        symbol="اهرم",
        category=MarketInstrument.Category.ETF,
    )
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.ETF_NAV,
        symbol="اهرم", date="1404-01-01", close_price=Decimal("24500"),
    )

    assert resolve_historical_price(asset, timezone.now()) == Decimal("24500")


def test_daily_bar_fallback_ignores_a_same_symbol_bar_from_another_feed():
    from marketdata.models import MarketDailyBar, MarketInstrument
    from portfolio.services.ledger import PriceResolutionError, resolve_historical_price

    asset = Asset.objects.create(
        key="leverage_etf2", name="Leverage ETF", is_active=True,
        asset_class=Asset.AssetClass.STOCK, tse_symbol="اهرم",
    )
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC,
        symbol="اهرم",
        category=MarketInstrument.Category.ETF,
    )
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.INDEX,
        symbol="اهرم", date="1404-01-01", close_price=Decimal("24500"),
    )

    with pytest.raises(PriceResolutionError):
        resolve_historical_price(asset, timezone.now())


def test_daily_bar_fallback_skips_a_row_the_warehouse_rejected():
    """A bar the warehouse flagged bad must not reach a trade price -- valuation
    already drops it, and the two readers have to agree."""
    from marketdata.models import (
        ArchiveFetchState, MarketDailyBar, MarketInstrument, RejectedRecord,
    )
    from portfolio.services.ledger import resolve_historical_price

    asset = Asset.objects.create(
        key="leverage_etf3", name="Leverage ETF", is_active=True,
        asset_class=Asset.AssetClass.STOCK, tse_symbol="اهرم",
    )
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC,
        symbol="اهرم",
        category=MarketInstrument.Category.ETF,
    )
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.ETF_NAV,
        symbol="اهرم", date="1404-01-01", close_price=Decimal("24500"),
    )
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.ETF_NAV,
        symbol="اهرم", date="1404-01-02", close_price=Decimal("1"),
    )
    RejectedRecord.objects.create(
        endpoint=ArchiveFetchState.Endpoint.ETF_NAV_DAILY,
        symbol="اهرم", date="1404-01-02", reason="close_outside_range",
    )

    assert resolve_historical_price(asset, timezone.now()) == Decimal("24500")


# --- Real estate: entering and correcting a property ------------------------
#
# Both bugs below reached the user. Adding or revaluing a property on an account
# whose baseline was already set raised a LedgerError that escaped as a 500 (the
# holdings screen showed a bare "Something went wrong"), and the ledger's edit
# endpoint accepted a new size, dropped it, and answered 200 -- a resize that
# looked saved and was not.


def _add_property(account, name="Apartment", area="91", price="100", occurred_at=None):
    body = {"new_property_name": name, "area_sqm": area, "price_per_sqm_million": price}
    if occurred_at is not None:
        body["occurred_at"] = occurred_at
    return _client(account.user).post(
        f"/api/accounts/{account.id}/holdings/",
        body,
        format="json",
    )


@pytest.mark.django_db
def test_a_property_can_be_added_after_tracking_has_begun(account, asset_catalog):
    # `account` already carries an opening-cash baseline, which is what made the
    # new property's opening entry illegal.
    response = _add_property(account)

    assert response.status_code == 201, response.data
    holding = Holding.objects.get(pk=response.data["id"])
    assert holding.area_sqm == Decimal("91")
    assert holding.quantity == Decimal("100")


@pytest.mark.django_db
def test_a_portfolio_can_hold_more_than_one_property(account, asset_catalog):
    first = _add_property(account, name="Apartment", area="91", price="100")
    second = _add_property(account, name="Villa", area="200", price="80")

    assert (first.status_code, second.status_code) == (201, 201), second.data
    sizes = sorted(
        h.area_sqm for h in account.holdings.filter(asset__is_house=True)
    )
    assert sizes == [Decimal("91.00"), Decimal("200.00")]


@pytest.mark.django_db
def test_resizing_a_property_from_the_holdings_screen(account, asset_catalog):
    holding = Holding.objects.get(pk=_add_property(account).data["id"])

    response = _client(account.user).patch(
        f"/api/accounts/{account.id}/holdings/{holding.id}/",
        {"quantity": "150", "area_sqm": "120"},
        format="json",
    )

    assert response.status_code == 200, response.data
    holding.refresh_from_db()
    assert (holding.quantity, holding.area_sqm) == (Decimal("150"), Decimal("120.00"))


@pytest.mark.django_db
def test_resizing_a_property_from_the_ledger(account, asset_catalog):
    holding = Holding.objects.get(pk=_add_property(account).data["id"])
    entry = LedgerEntry.objects.get(account=account, asset=holding.asset)

    response = _client(account.user).patch(
        f"/api/accounts/{account.id}/ledger/{entry.id}/",
        {"area_sqm": "120"},
        format="json",
    )

    assert response.status_code == 200, response.data
    holding.refresh_from_db()
    assert holding.area_sqm == Decimal("120.00")
    # Repricing must not silently resize, and resizing must not silently reprice.
    assert holding.quantity == Decimal("100")


@pytest.mark.django_db
def test_repricing_from_the_ledger_keeps_the_size(account, asset_catalog):
    holding = Holding.objects.get(pk=_add_property(account).data["id"])
    entry = LedgerEntry.objects.get(account=account, asset=holding.asset)

    response = _client(account.user).patch(
        f"/api/accounts/{account.id}/ledger/{entry.id}/",
        {"quantity": "150"},
        format="json",
    )

    assert response.status_code == 200, response.data
    holding.refresh_from_db()
    assert (holding.quantity, holding.area_sqm) == (Decimal("150"), Decimal("91.00"))


@pytest.mark.django_db
def test_only_a_property_entry_accepts_a_size(account, asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("176000000")})
    execute_trade(
        account=account, asset=asset_catalog["emami_coin"], side="buy",
        quantity=Decimal("1"),
    )
    entry = LedgerEntry.objects.filter(
        account=account, kind=LedgerEntry.Kind.BUY
    ).latest("pk")

    response = _client(account.user).patch(
        f"/api/accounts/{account.id}/ledger/{entry.id}/",
        {"area_sqm": "120"},
        format="json",
    )

    assert response.status_code == 400, response.data


@pytest.mark.django_db
def test_a_rejected_property_mark_is_explained_not_a_500(account, asset_catalog):
    """A ledger rule the user trips must arrive as a readable 400."""
    holding = Holding.objects.get(pk=_add_property(account).data["id"])

    response = _client(account.user).patch(
        f"/api/accounts/{account.id}/holdings/{holding.id}/",
        {"quantity": "150", "occurred_at": "2999-01-01T00:00:00Z"},
        format="json",
    )

    assert response.status_code == 400, response.data
    assert "future" in str(response.data).lower()


@pytest.mark.django_db
def test_deleting_a_property_removes_it(account, asset_catalog):
    holding = Holding.objects.get(pk=_add_property(account).data["id"])
    asset_id = holding.asset_id

    response = _client(account.user).delete(
        f"/api/accounts/{account.id}/holdings/{holding.id}/"
    )

    assert response.status_code in (200, 204), getattr(response, "data", response)
    assert not Holding.objects.filter(account=account, asset_id=asset_id).exists()


@pytest.mark.django_db
def test_deleting_a_revalued_property_removes_it(account, asset_catalog):
    holding = Holding.objects.get(pk=_add_property(account).data["id"])
    _client(account.user).patch(
        f"/api/accounts/{account.id}/holdings/{holding.id}/",
        {"quantity": "150"},
        format="json",
    )
    asset_id = holding.asset_id

    response = _client(account.user).delete(
        f"/api/accounts/{account.id}/holdings/{holding.id}/"
    )

    assert response.status_code in (200, 204), getattr(response, "data", response)
    assert not Holding.objects.filter(account=account, asset_id=asset_id).exists()

@pytest.mark.django_db
def test_property_create_stamps_the_purchase_date_not_today(account, asset_catalog):
    """Unit/API: the create path is the trust boundary for occurred_at."""
    when = timezone.now() - datetime.timedelta(days=3650)
    response = _add_property(account, occurred_at=when.isoformat())
    assert response.status_code == 201, response.data
    holding = Holding.objects.get(pk=response.data["id"])
    entry = LedgerEntry.objects.get(account=account, asset=holding.asset)
    assert entry.timestamp.date() == when.date()


@pytest.mark.django_db
def test_backdated_property_is_written_into_older_snapshots(account, asset_catalog):
    """Unit: snapshots taken before the holding existed must include the house."""
    old = timezone.now() - datetime.timedelta(days=30)
    snap = Snapshot.objects.create(
        user=account.user, account=account, total_value_tomans=Decimal("100"),
        timestamp=old,
    )
    when = timezone.now() - datetime.timedelta(days=3650)
    response = _add_property(account, occurred_at=when.isoformat())
    assert response.status_code == 201, response.data
    snap.refresh_from_db()
    assert snap.total_value_tomans == Decimal("9100000100")



@pytest.mark.django_db
def test_restamping_a_property_does_not_double_count_existing_snapshots(
    account, asset_catalog
):
    """`backfill_house_into_snapshots` ADDS to each row in place and keeps no
    record that it ran. Snapshots from the day the house first became visible
    already contain it -- from the Holding row if the price loop photographed
    it, or from the backfill that ran when the property was created. Passing a
    later bound (holding.created_at) re-adds the house to every one of those.
    """
    from portfolio.services.ledger import HOUSE_MARK_KINDS

    added = _add_property(account, name="Tehran", area="100", price="350")
    assert added.status_code == 201, added.data
    holding = Holding.objects.get(pk=added.data["id"])
    mark = LedgerEntry.objects.get(
        account=account, asset=holding.asset, kind__in=HOUSE_MARK_KINDS,
        reversal_of__isnull=True, reversed_by__isnull=True,
    )
    house_value = Decimal("350") * Decimal("1000000") * Decimal("100")

    # One snapshot from before the house existed, one from after.
    old = Snapshot.objects.create(
        user=account.user, account=account, total_value_tomans=Decimal("1000"),
    )
    Snapshot.objects.filter(pk=old.pk).update(
        timestamp=mark.timestamp - dt.timedelta(days=30)
    )
    recent = Snapshot.objects.create(
        user=account.user, account=account,
        total_value_tomans=Decimal("1000") + house_value,
    )
    Snapshot.objects.filter(pk=recent.pk).update(
        timestamp=mark.timestamp + dt.timedelta(days=1)
    )

    target = (mark.timestamp - dt.timedelta(days=60)).date().isoformat()
    call_command(
        "restamp_house_marks", holding_id=holding.id, occurred_at=target,
    )

    old.refresh_from_db()
    recent.refresh_from_db()
    assert old.total_value_tomans == Decimal("1000") + house_value, (
        "a snapshot predating the house must gain it once"
    )
    assert recent.total_value_tomans == Decimal("1000") + house_value, (
        "a snapshot that already contained the house must not gain it again"
    )

    mark.refresh_from_db()
    assert mark.timestamp.date().isoformat() == target
    assert mark.kind in HOUSE_MARK_KINDS


@pytest.mark.django_db
def test_restamping_an_opening_converts_it_to_a_valuation_mark(account, asset_catalog):
    """Every opening on an account must share `tracking_started_at`
    (create_ledger_entry enforces it). Moving one property's opening to its real
    purchase date would desynchronise it from the account's other openings and
    make the next opening write fail. For a house the two kinds are equivalent --
    `_projection_state` and `house_state_as_of` both REPLACE on either -- so the
    moved mark becomes a valuation mark instead.
    """
    from portfolio.services.ledger import HOUSE_MARK_KINDS

    account.refresh_from_db()
    baseline = account.tracking_started_at
    assert baseline is not None, "the fixture's opening-cash baseline"

    added = _add_property(
        account, name="Lahijan", area="74", price="100",
        occurred_at=baseline.isoformat(),
    )
    assert added.status_code == 201, added.data
    holding = Holding.objects.get(pk=added.data["id"])
    mark = LedgerEntry.objects.get(
        account=account, asset=holding.asset, kind__in=HOUSE_MARK_KINDS,
        reversal_of__isnull=True, reversed_by__isnull=True,
    )
    assert mark.kind == LedgerEntry.Kind.OPENING_POSITION, "precondition"

    call_command(
        "restamp_house_marks", holding_id=holding.id,
        occurred_at=(baseline - dt.timedelta(days=100)).date().isoformat(),
    )

    mark.refresh_from_db()
    assert mark.kind == LedgerEntry.Kind.VALUATION_MARK
    account.refresh_from_db()
    assert account.tracking_started_at == baseline, (
        "the account's own baseline must not move with one property"
    )


# --- What the ledger's Amount / Price / Value columns are worth --------------
#
# Integration, not unit: the defect was in what the LIST endpoint assembles out
# of the serializer and the synthetic-holding rows, and neither half was wrong on
# its own. `amount_tomans` is only stored for the kinds that move money, so every
# position the user merely declared -- a property, a manual gold bar, an opening
# -- printed a quantity and a price with an empty Value beside them.


def _ledger_rows(account):
    response = _client(account.user).get(f"/api/accounts/{account.id}/ledger/")
    assert response.status_code == 200, response.data
    return {row["asset_key"]: row for row in response.data if row.get("asset_key")}


@pytest.mark.django_db
def test_ledger_columns_multiply_out_for_declared_positions(
    account, asset_catalog, write_prices
):
    write_prices({"swiss_gold_bar_1g": Decimal("7500000")})
    account.refresh_from_db()  # the fixture's opening-cash baseline
    client = _client(account.user)
    # A property: 91 sqm at 100 million Toman/sqm.
    property_holding = Holding.objects.get(pk=_add_property(account).data["id"])
    # A manual asset entered from the holdings screen, which writes no ledger row.
    bar = client.post(
        f"/api/accounts/{account.id}/holdings/",
        {"asset_key": "swiss_gold_bar_1g", "quantity": "4",
         "unit_price_tomans": "7500000"},
        format="json",
    )
    assert bar.status_code == 201, bar.data
    # A stock the user already owned, quoted in Rial.
    create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.OPENING_POSITION,
        asset=asset_catalog["kama_stock"],
        quantity=Decimal("100"),
        unit_price_tomans=Decimal("5330"),
        occurred_at=account.tracking_started_at,
    )

    rows = _ledger_rows(account)

    house = rows[property_holding.asset.key]
    assert Decimal(house["value_tomans"]) == Decimal("91") * Decimal("100") * 10**6
    assert Decimal(house["area_sqm"]) == Decimal("91"), "the Amount column"
    assert Decimal(house["quantity"]) == Decimal("100"), "the Price column, per sqm"

    gold = rows["swiss_gold_bar_1g"]
    assert Decimal(gold["unit_price_tomans"]) == Decimal("7500000")
    assert Decimal(gold["value_tomans"]) == Decimal("4") * Decimal("7500000")

    # Rial price, Toman value: the division lands on the product, so the column
    # is a tenth of the number the two beside it multiply to.
    stock = rows["kama_stock"]
    assert stock["unit_price_currency"] == "rial"
    assert Decimal(stock["value_tomans"]) == Decimal("100") * Decimal("5330") / 10
    assert stock["label"] == "کاما", "the ticker, not the company name"


@pytest.mark.django_db
def test_a_stock_buy_debits_cash_in_toman_not_rial(account, asset_catalog, write_prices):
    """`amount_tomans` is a quantity x price product, and TSE prices are Rial.

    Under the old 1/10-share convention `qty x rial` happened to land on Toman,
    so this column was correct without ever converting -- which is why it did
    not look like a product. With true share counts an unconverted amount is
    Rial, and it is read as Toman by the cash replay, by `timeline.cash_as_of`,
    and therefore by every TWR cash-flow boundary. Ten times too much money
    leaves the account on every stock purchase.
    """
    write_prices({"kama_stock": Decimal("5330")})
    account.track_cash = True
    account.save(update_fields=["track_cash"])
    account.refresh_from_db()
    opening_cash = account.cash_balance_tomans

    entry = create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.BUY,
        asset=asset_catalog["kama_stock"],
        quantity=Decimal("1000"),
        unit_price_tomans=Decimal("5330"),
    )

    # 1,000 shares x 5,330 Rial = 5,330,000 Rial = 533,000 Toman.
    assert entry.amount_tomans == Decimal("533000.0000")
    account.refresh_from_db()
    assert account.cash_balance_tomans == opening_cash - Decimal("533000")


@pytest.mark.django_db
def test_ledger_and_performance_report_the_same_stock_pnl(
    account, asset_catalog, write_prices
):
    """Both compute quantity x price-delta. Only one of them converted, so the
    Ledger page and the Performance page disagreed ten-fold on one position.
    """
    from portfolio.services.ledger import entry_pnl_map
    from portfolio.services.performance import _position_metrics

    write_prices({"kama_stock": Decimal("5430")})
    entry = create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.BUY,
        asset=asset_catalog["kama_stock"],
        quantity=Decimal("1000"),
        unit_price_tomans=Decimal("5330"),
    )

    pnl = entry_pnl_map(
        list(account.transactions.select_related("asset").order_by("timestamp", "pk")),
        {"kama_stock": Decimal("5430")},
    )[entry.pk]
    metrics = _position_metrics(account)["kama_stock"]

    assert pnl["pnl_kind"] == "unrealized"
    # 1,000 x (5,430 - 5,330) Rial = 100,000 Rial = 10,000 Toman.
    assert Decimal(pnl["pnl_tomans"]) == Decimal("10000")
    assert Decimal(metrics["unrealized_pnl_tomans"]) == Decimal(pnl["pnl_tomans"])


@pytest.mark.django_db
def test_a_house_backfill_can_be_undone_exactly(account, asset_catalog):
    """`backfill_house_into_snapshots` has no marker recording that it ran, so
    applying it to rows that already contained the house doubles it. Synthetic
    gap-fill snapshots value the THEN-CURRENT holdings, so they hold every
    property that existed when the gap-fill ran, whatever their date. The
    inverse has to land back on the original number to the rial.
    """
    from portfolio.services.ledger import (
        backfill_house_into_snapshots, remove_house_from_snapshots,
    )

    added = _add_property(account, name="Tehran", area="100", price="350")
    holding = Holding.objects.get(pk=added.data["id"])
    original = Decimal("1000")
    snap = Snapshot.objects.create(
        user=account.user, account=account, total_value_tomans=original,
    )
    cutoff = timezone.now() + dt.timedelta(days=1)

    added_rows = backfill_house_into_snapshots(
        account, holding.asset, before=cutoff,
    )
    snap.refresh_from_db()
    assert added_rows == 1
    assert snap.total_value_tomans > original

    removed_rows = remove_house_from_snapshots(
        account, holding.asset, before=cutoff,
    )
    snap.refresh_from_db()
    assert removed_rows == added_rows
    assert snap.total_value_tomans == original, "the inverse must be exact"


@pytest.mark.django_db
def test_a_rights_issue_dilutes_cost_basis_instead_of_voiding_it(
    account, asset_catalog, write_prices
):
    """افزایش سرمایه hands over free shares. The money already spent now buys
    more of them, so the average cost per share falls and the basis stays known.

    Recording it as an opening_position would mark the basis unknown and throw
    away the real purchase history; recording it as a zero-price buy trips the
    `price_tomans <= 0` "no price recorded" sentinel to the same effect. Either
    one loses the breakeven price, which is the number the owner actually wants.
    """
    from portfolio.services.performance import _position_metrics

    write_prices({"kama_stock": Decimal("5330")})
    kama = asset_catalog["kama_stock"]
    create_ledger_entry(
        account=account, kind=LedgerEntry.Kind.BUY, asset=kama,
        quantity=Decimal("1000"), unit_price_tomans=Decimal("4000"),
    )
    create_ledger_entry(
        account=account, kind=LedgerEntry.Kind.RIGHTS_ISSUE, asset=kama,
        quantity=Decimal("1000"), note="افزایش سرمایه",
    )

    account.refresh_from_db()
    holding = account.holdings.get(asset=kama)
    assert holding.quantity == Decimal("2000"), "free shares still count as shares"

    metrics = _position_metrics(account)["kama_stock"]
    assert metrics["cost_basis_known"] is True
    # 1,000 x 4,000 Rial spread over 2,000 shares = 2,000 Rial each.
    assert Decimal(metrics["average_cost_tomans"]) == Decimal("2000")
    # Cash basis is unchanged by the issue: 4,000,000 Rial = 400,000 Toman.
    assert Decimal(metrics["total_cost_basis_tomans"]) == Decimal("400000")


def test_adding_an_owned_asset_after_the_baseline_is_not_refused(
    ledger_account, asset_catalog, write_prices
):
    """"I already own this" used to 400 with "Opening entries must share the
    tracking start timestamp" on any account that had a baseline -- accurate
    about the invariant, and nothing the user could act on. The date now picks
    the kind: at-or-before the baseline it is baseline, after it is a buy.
    """
    write_prices({"emami_coin": Decimal("100"), "half_coin": Decimal("50")})
    client = _client(ledger_account.user)
    started_at = timezone.now() - datetime.timedelta(days=10)

    baseline = _post(client, ledger_account, {
        "kind": "opening_position",
        "asset_key": "emami_coin",
        "quantity": "2",
        "occurred_at": started_at.isoformat(),
    })
    assert baseline.status_code == 201, baseline.data
    assert baseline.data["kind"] == "opening_position"

    # Declared after the baseline: still an opening, snapped onto the baseline.
    # NOT a buy -- a buy is a funded purchase and would debit cash that never
    # moved on any account that tracks a balance.
    later = _post(client, ledger_account, {
        "kind": "opening_position",
        "asset_key": "half_coin",
        "quantity": "3",
        "occurred_at": (timezone.now() - datetime.timedelta(days=2)).isoformat(),
    })
    assert later.status_code == 201, later.data
    assert later.data["kind"] == "opening_position"

    # Predates tracking: still part of the baseline, so it snaps onto it rather
    # than desynchronising the account's other openings.
    earlier = _post(client, ledger_account, {
        "kind": "opening_position",
        "asset_key": "quarter_coin",
        "quantity": "5",
        "occurred_at": (started_at - datetime.timedelta(days=30)).isoformat(),
    })
    assert earlier.status_code == 201, earlier.data
    assert earlier.data["kind"] == "opening_position"

    ledger_account.refresh_from_db()
    assert ledger_account.tracking_started_at == started_at
    holdings = {
        h.asset.key: h.quantity
        for h in Holding.objects.filter(account=ledger_account).select_related("asset")
    }
    assert holdings == {
        "emami_coin": Decimal("2"), "half_coin": Decimal("3"),
        "quarter_coin": Decimal("5"),
    }


def test_declaring_a_holding_you_already_own_never_spends_cash(
    ledger_account, asset_catalog, write_prices
):
    """Booking it as a buy would settle against the balance the moment an
    account tracks cash -- draining money that never moved, or failing the
    replay outright. Nothing entered the portfolio; only the record of it did.
    """
    write_prices({"emami_coin": Decimal("100"), "half_coin": Decimal("1000")})
    client = _client(ledger_account.user)
    started_at = (timezone.now() - datetime.timedelta(days=10)).isoformat()

    assert _post(client, ledger_account, {
        "kind": "opening_cash", "amount_tomans": "5000", "occurred_at": started_at,
    }).status_code == 201
    assert _post(client, ledger_account, {
        "kind": "opening_position", "asset_key": "emami_coin",
        "quantity": "2", "occurred_at": started_at,
    }).status_code == 201

    ledger_account.refresh_from_db()
    before = ledger_account.cash_balance_tomans

    # 3 half-coins at 1,000 would be 3,000 Toman of the 5,000 balance.
    declared = _post(client, ledger_account, {
        "kind": "opening_position", "asset_key": "half_coin",
        "quantity": "3",
        "occurred_at": (timezone.now() - datetime.timedelta(days=1)).isoformat(),
    })

    assert declared.status_code == 201, declared.data
    ledger_account.refresh_from_db()
    assert ledger_account.cash_balance_tomans == before
    assert Holding.objects.get(
        account=ledger_account, asset=asset_catalog["half_coin"]
    ).quantity == Decimal("3")


def test_a_dollar_quoted_live_price_is_converted_before_it_becomes_a_ledger_price():
    """Unit test: one conversion rule, checked where it turns into durable data.

    `bitcoin_usd` is quoted and stored in DOLLARS (see `returns.USD_QUOTED_KEYS`).
    With no daily bar to fall back on, the resolver reached the raw Price row and
    wrote ~95,000 into `LedgerEntry.price_tomans` -- the same mistake
    `currency.to_toman` exists to refuse, on a path that never calls it.
    """
    from portfolio.services.ledger import resolve_historical_price

    asset = Asset.objects.create(
        key="bitcoin_usd", name="Bitcoin", is_active=True,
        asset_class=Asset.AssetClass.CRYPTO,
    )
    usd = Asset.objects.create(
        key="usd_cash", name="US Dollar", is_active=True,
        asset_class=Asset.AssetClass.CASH,
    )
    Price.objects.create(asset=asset, price=Decimal("95000"), source="API")
    Price.objects.create(asset=usd, price=Decimal("60000"), source="API")

    assert resolve_historical_price(asset, timezone.now()) == Decimal("5700000000")


def test_a_dollar_quote_with_no_rate_refuses_rather_than_storing_dollars():
    from portfolio.services.ledger import PriceResolutionError, resolve_historical_price

    asset = Asset.objects.create(
        key="gold_ounce_usd", name="Gold Ounce", is_active=True,
        asset_class=Asset.AssetClass.GOLD,
    )
    Price.objects.create(asset=asset, price=Decimal("2400"), source="API")

    with pytest.raises(PriceResolutionError):
        resolve_historical_price(asset, timezone.now())


def test_a_trade_dated_today_books_at_the_live_price_not_yesterdays_close():
    """Unit test: the warehouse has no close for today until after the bell.

    The wizard promises "the market price", then the dashboard values the new
    holding at the live one -- so booking yesterday's close wrote a cost basis
    that was wrong on arrival and showed a gain on a position seconds old.
    """
    from portfolio.services.ledger import resolve_historical_price

    asset = Asset.objects.create(
        key="quarter_coin", name="Quarter coin", is_active=True,
        asset_class=Asset.AssetClass.GOLD, brs_symbol="IR_COIN_QUARTER",
    )
    yesterday = jdatetime.date.fromgregorian(
        date=(timezone.now() - dt.timedelta(days=1)).date()
    ).strftime("%Y-%m-%d")
    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_QUARTER", date=yesterday,
        close_price=Decimal("30000000"), unit="تومان",
    )
    Price.objects.create(asset=asset, price=Decimal("31000000"), source="API")

    assert resolve_historical_price(asset, timezone.now()) == Decimal("31000000")


def test_a_backdated_trade_still_reads_the_warehouse():
    from portfolio.services.ledger import resolve_historical_price

    asset = Asset.objects.create(
        key="half_coin", name="Half coin", is_active=True,
        asset_class=Asset.AssetClass.GOLD, brs_symbol="IR_COIN_HALF",
    )
    when = timezone.now() - dt.timedelta(days=3)
    j_when = jdatetime.date.fromgregorian(date=when.date()).strftime("%Y-%m-%d")
    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_HALF", date=j_when,
        close_price=Decimal("50000000"), unit="تومان",
    )
    # A live row exists but says nothing about a day three days gone.
    Price.objects.create(asset=asset, price=Decimal("99000000"), source="API")

    assert resolve_historical_price(asset, when) == Decimal("50000000")

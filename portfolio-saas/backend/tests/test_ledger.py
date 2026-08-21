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
from portfolio.models import Account, Asset, Holding, Transaction
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


def test_buy_cannot_make_cash_negative(ledger_account, asset_catalog):
    client = _client(ledger_account.user)
    response = _post(client, ledger_account, {
        "kind": "buy",
        "asset_key": "emami_coin",
        "quantity": "1",
        "unit_price_tomans": "100",
        "occurred_at": timezone.now().isoformat(),
    })

    assert response.status_code == 400
    assert response.data["detail"] == "Insufficient cash balance."
    assert not LedgerEntry.objects.filter(account=ledger_account).exists()


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

    def test_tradeable_holding_cannot_bypass_ledger(self, account, asset_catalog):
        client = self._client(account.user)
        resp = client.post(
            f"/api/accounts/{account.id}/holdings/",
            {"asset_key": "emami_coin", "quantity": "2"},
            format="json",
        )
        assert resp.status_code == 400
        assert not account.holdings.filter(asset__key="emami_coin").exists()

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

    def test_tradeable_holding_patch_rejected(self, account, asset_catalog, write_prices):
        write_prices({"emami_coin": Decimal("176000000")})
        execute_trade(account=account, asset=asset_catalog["emami_coin"], side="buy", quantity=Decimal("1"))
        holding = Holding.objects.get(account=account, asset=asset_catalog["emami_coin"])
        response = self._client(account.user).patch(
            f"/api/accounts/{account.id}/holdings/{holding.id}/",
            {"quantity": "2"},
            format="json",
        )
        assert response.status_code == 400

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

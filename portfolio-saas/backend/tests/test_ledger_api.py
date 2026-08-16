"""Account-ledger API integration tests.

Integration tests fit here because one request must keep the immutable ledger,
holding projection, cash projection, ownership boundary, and reversal together.
"""
import datetime
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from portfolio.models import Account, Holding, LedgerEntry


pytestmark = pytest.mark.django_db


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


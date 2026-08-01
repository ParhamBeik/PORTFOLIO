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
    started_at = timezone.now() - datetime.timedelta(days=30)
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
    assert response.data == {
        "performance_available": False,
        "detail": "Complete an opening baseline before calculating performance.",
    }


def test_deposit_is_external_but_does_not_create_investment_return(ledger_account):
    client = _client(ledger_account.user)
    started_at = timezone.now() - datetime.timedelta(days=30)
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

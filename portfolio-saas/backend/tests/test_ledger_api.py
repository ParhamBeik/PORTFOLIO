"""Account-ledger API integration tests.

Integration tests fit here because one request must keep the immutable ledger,
holding projection, cash projection, ownership boundary, and reversal together.
"""
import datetime
from decimal import Decimal

import pytest
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

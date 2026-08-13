import csv
import io
import json
import zipfile
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import User
from portfolio.models import Account, Asset, Holding, LedgerEntry


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


def _register_payload(email="beta@test.test"):
    return {
        "email": email,
        "password": "Sup3rSecret!",
        "first_name": "Beta",
        "last_name": "User",
    }


def test_export_is_scoped_zip_without_password_or_tokens(make_user):
    user = make_user(email="export@test.test")
    other = make_user(email="other-export@test.test")
    account = Account.objects.create(user=user, name="Main")
    Account.objects.create(user=other, name="Other")
    asset = Asset.objects.create(
        key="export_asset", name="Export asset", is_active=False
    )
    Holding.objects.create(account=account, asset=asset, quantity=2)
    LedgerEntry.objects.create(
        account=account,
        asset=asset,
        kind=LedgerEntry.Kind.OPENING_POSITION,
        quantity=2,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get("/api/auth/export/")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {
            "manifest.json",
            "profile.csv",
            "accounts.csv",
            "ledger.csv",
            "holdings.csv",
            "imports.csv",
        } <= names
        payload = b"".join(archive.read(name) for name in names).decode()
    assert user.email in payload
    assert other.email not in payload
    assert "password" not in payload.lower()
    assert "refresh" not in payload.lower()


def test_deletion_requires_password(make_user):
    user = make_user(email="delete@test.test")
    account = Account.objects.create(user=user, name="Main")
    client = APIClient()
    client.force_authenticate(user=user)

    denied = client.delete(
        "/api/auth/me/",
        {"password": "wrong", "confirmation": "DELETE"},
        format="json",
    )
    deleted = client.delete(
        "/api/auth/me/",
        {"password": "Sup3rSecret!", "confirmation": "DELETE"},
        format="json",
    )

    assert denied.status_code == 400
    assert deleted.status_code == 204
    assert deleted.cookies["ps_refresh"]["max-age"] == 0
    assert not User.objects.filter(pk=user.pk).exists()
    assert not Account.objects.filter(pk=account.pk).exists()

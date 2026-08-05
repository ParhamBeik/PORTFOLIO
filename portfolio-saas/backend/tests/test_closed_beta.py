import csv
import io
import json
import zipfile
import pytest
from django.core import mail
from django.core.cache import cache
from django.utils import timezone
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


def test_verification_activates_account_and_login():
    from accounts.services import make_email_verification_token

    client = APIClient()
    registered = client.post(
        "/api/auth/register/",
        _register_payload(email="verify@test.test"),
        format="json",
    )
    user = User.objects.get(email="verify@test.test")
    assert registered.status_code == 201
    assert user.is_active is False
    assert user.email_verified_at is None
    assert len(mail.outbox) == 1

    denied = client.post(
        "/api/auth/login/",
        {"email": user.email, "password": "Sup3rSecret!"},
        format="json",
    )
    verified = client.post(
        "/api/auth/verify-email/",
        {"token": make_email_verification_token(user)},
        format="json",
    )
    login = client.post(
        "/api/auth/login/",
        {"email": user.email, "password": "Sup3rSecret!"},
        format="json",
    )
    assert denied.status_code == 401
    assert verified.status_code == 200
    assert login.status_code == 200
    user.refresh_from_db()
    assert user.is_active is True
    assert user.email_verified_at is not None


def test_resend_verification_is_rate_limited():
    user = User.objects.create_user(
        email="resend@test.test",
        password="Sup3rSecret!",
        is_active=False,
        email_verified_at=None,
    )
    client = APIClient()
    first = client.post(
        "/api/auth/resend-verification/", {"email": user.email}, format="json"
    )
    second = client.post(
        "/api/auth/resend-verification/", {"email": user.email}, format="json"
    )
    assert first.status_code == 200
    assert second.status_code == 429


def test_password_reset_expires_and_revokes_refresh_tokens(make_user):
    from accounts.services import make_password_reset_token
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

    user = make_user(email="reset@test.test")
    client = APIClient()
    client.post(
        "/api/auth/login/",
        {"email": user.email, "password": "Sup3rSecret!"},
        format="json",
    )
    uid, token = make_password_reset_token(user)
    response = client.post(
        "/api/auth/password-reset/confirm/",
        {
            "uid": uid,
            "token": token,
            "new_password": "N3wSecretPass123!",
            "confirm_password": "N3wSecretPass123!",
        },
        format="json",
    )
    assert response.status_code == 200
    assert response.cookies["ps_refresh"]["max-age"] == 0
    assert BlacklistedToken.objects.filter(token__user=user).exists()


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

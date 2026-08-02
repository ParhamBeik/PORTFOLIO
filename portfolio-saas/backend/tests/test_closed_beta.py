import csv
import io
import json
import zipfile
from datetime import timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Invitation, User
from billing.models import Payment
from portfolio.models import Account, Asset, BacktestRun, Holding, LedgerEntry


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


def _register_payload(token, email="beta@test.test"):
    return {
        "email": email,
        "password": "Sup3rSecret!",
        "first_name": "Beta",
        "invite_token": token,
    }


def test_invite_is_email_bound_expiring_and_single_use():
    invite, token = Invitation.issue(email="bound@test.test")
    client = APIClient()

    mismatch = client.post(
        "/api/auth/register/",
        _register_payload(token, email="other@test.test"),
        format="json",
    )
    assert mismatch.status_code == 400

    ok = client.post(
        "/api/auth/register/",
        _register_payload(token, email="bound@test.test"),
        format="json",
    )
    replay = client.post(
        "/api/auth/register/",
        _register_payload(token, email="bound2@test.test"),
        format="json",
    )
    assert ok.status_code == 201
    assert replay.status_code == 400

    expired, expired_token = Invitation.issue(email="expired@test.test")
    Invitation.objects.filter(pk=expired.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    response = client.post(
        "/api/auth/register/",
        _register_payload(expired_token, email="expired@test.test"),
        format="json",
    )
    assert response.status_code == 400


def test_verification_activates_account_and_login():
    from accounts.services import make_email_verification_token

    _invite, raw = Invitation.issue(email="verify@test.test")
    client = APIClient()
    registered = client.post(
        "/api/auth/register/",
        _register_payload(raw, email="verify@test.test"),
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
    BacktestRun.objects.create(
        user=user, params_hash="p", universe_hash="u", manifest={"safe": True}
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
            "payments.csv",
            "backtests.csv",
        } <= names
        payload = b"".join(archive.read(name) for name in names).decode()
    assert user.email in payload
    assert other.email not in payload
    assert "password" not in payload.lower()
    assert "refresh" not in payload.lower()


def test_deletion_requires_password_and_pseudonymizes_payments(make_user):
    user = make_user(email="delete@test.test")
    account = Account.objects.create(user=user, name="Main")
    Payment.objects.create(
        user=user, authority="DELETE-AUTH", amount_rial=1000
    )
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
    payment = Payment.objects.get(authority="DELETE-AUTH")
    assert payment.user_id is None
    assert payment.former_customer_id is not None

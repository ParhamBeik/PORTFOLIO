"""Registration, login, JWT lifecycle, closed-beta gating, and the rule that one user never sees another's account.

Merged from 3 files; each section keeps its original banner.
"""

import csv
from datetime import timedelta
from decimal import Decimal
import io
import json

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
import pytest
from rest_framework.test import APIClient
import zipfile

from accounts.models import User
from marketdata.models import AssetMetricSnapshot
from portfolio.models import Account, Asset, Holding, LedgerEntry, Snapshot
from portfolio.services.insights import net_worth_trend

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_auth.py
# Authentication: register, login, JWT-protected /me/.


@pytest.fixture(autouse=True)
def clear_auth_throttles():
    cache.clear()


def test_register_is_closed_without_creating_a_user():
    client = APIClient()
    resp = client.post(
        "/api/auth/register/",
        {"email": "new@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "New memberships are currently closed."
    assert not User.objects.filter(email="new@test.test").exists()


def test_login_returns_access_and_sets_refresh_cookie():
    from accounts.models import User

    User.objects.create_user(email="login@test.test", password="Sup3rSecret!")
    resp = APIClient().post(
        "/api/auth/login/",
        {"email": "login@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access" in data
    assert "refresh" not in data
    assert "session_expires_at" in data
    assert resp.cookies["ps_refresh"]["httponly"] is True


def test_access_token_lifetime_is_thirty_minutes():
    from rest_framework_simplejwt.settings import api_settings

    assert api_settings.ACCESS_TOKEN_LIFETIME == timedelta(minutes=30)


def test_cookie_refresh_requires_csrf_and_rotates_cookie(make_user):
    make_user(email="refresh-cookie@test.test")
    client = APIClient(enforce_csrf_checks=True)
    login_response = client.post(
        "/api/auth/login/",
        {"email": "refresh-cookie@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    csrf = client.cookies["csrftoken"].value

    denied = client.post("/api/token/refresh/", {}, format="json")
    refreshed = client.post(
        "/api/token/refresh/", {}, format="json", HTTP_X_CSRFTOKEN=csrf
    )

    assert denied.status_code == 403
    assert refreshed.status_code == 200
    assert "access" in refreshed.json() and "refresh" not in refreshed.json()
    assert refreshed.cookies["ps_refresh"]["httponly"] is True


def test_login_wrong_password_rejected():
    from accounts.models import User

    User.objects.create_user(email="bad@test.test", password="Sup3rSecret!")
    resp = APIClient().post(
        "/api/auth/login/",
        {"email": "bad@test.test", "password": "nope"},
        format="json",
    )
    assert resp.status_code == 401


def test_me_requires_authentication():
    assert APIClient().get("/api/auth/me/").status_code == 401


def test_jwt_access_token_authenticates_me():
    """A real JWT (not force_authenticate) must resolve to the issuing user."""
    from accounts.models import User

    User.objects.create_user(email="jwt@test.test", password="Sup3rSecret!")
    client = APIClient()
    tokens = client.post(
        "/api/auth/login/",
        {"email": "jwt@test.test", "password": "Sup3rSecret!"},
        format="json",
    ).json()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    resp = client.get("/api/auth/me/")
    assert resp.status_code == 200
    assert resp.json()["email"] == "jwt@test.test"


@override_settings(REGISTRATION_OPEN=True)
def test_register_rejects_weak_all_numeric_password_over_http():
    response = APIClient().post(
        "/api/auth/register/",
        {"email": "weak@test.test", "password": "12345678"},
        format="json",
    )
    assert response.status_code == 400
    assert "password" in response.json()
    assert not User.objects.filter(email="weak@test.test").exists()


@override_settings(REGISTRATION_OPEN=True)
def test_register_rejects_too_short_password_over_http():
    response = APIClient().post(
        "/api/auth/register/",
        {"email": "short@test.test", "password": "Ab1!"},
        format="json",
    )
    assert response.status_code == 400
    assert "password" in response.json()
    assert not User.objects.filter(email="short@test.test").exists()


def test_update_user_profile(make_user):
    user = make_user(email="profile@test.test")
    user.first_name = "OldFirst"
    user.last_name = "OldLast"
    user.save()
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.patch(
        "/api/auth/me/",
        {"first_name": "NewFirst", "last_name": "NewLast"},
        format="json",
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["first_name"] == "NewFirst"
    assert data["last_name"] == "NewLast"
    user.refresh_from_db()
    assert user.first_name == "NewFirst"
    assert user.last_name == "NewLast"


def test_change_password_success(make_user):
    user = make_user(email="changepass@test.test")
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.post(
        "/api/auth/change-password/",
        {
            "old_password": "Sup3rSecret!",
            "new_password": "N3wSecretPass123!",
            "confirm_password": "N3wSecretPass123!",
        },
        format="json",
    )
    assert resp.status_code == 200
    assert resp.json()["detail"] == "Password updated successfully."

    user.refresh_from_db()
    assert user.check_password("N3wSecretPass123!")

    # Verify login with new password
    login_resp = APIClient().post(
        "/api/auth/login/",
        {"email": "changepass@test.test", "password": "N3wSecretPass123!"},
        format="json",
    )
    assert login_resp.status_code == 200


def test_change_password_revokes_existing_tokens(make_user):
    from accounts.serializers import PasswordAwareTokenRefreshSerializer
    from rest_framework.exceptions import AuthenticationFailed

    make_user(email="revoke@test.test")
    client = APIClient(enforce_csrf_checks=True)
    old_tokens = client.post(
        "/api/auth/login/",
        {"email": "revoke@test.test", "password": "Sup3rSecret!"},
        format="json",
    ).json()
    old_refresh = client.cookies["ps_refresh"].value
    csrf = client.cookies["csrftoken"].value
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {old_tokens['access']}")

    response = client.post(
        "/api/auth/change-password/",
        {
            "old_password": "Sup3rSecret!",
            "new_password": "N3wSecretPass123!",
            "confirm_password": "N3wSecretPass123!",
        },
        format="json",
    )

    assert response.status_code == 200
    new_tokens = response.json()

    client.credentials(HTTP_AUTHORIZATION=f"Bearer {old_tokens['access']}")
    assert client.get("/api/auth/me/").status_code == 401
    with pytest.raises(AuthenticationFailed):
        PasswordAwareTokenRefreshSerializer(
            data={"refresh": old_refresh}
        ).is_valid(raise_exception=True)

    client.credentials(HTTP_AUTHORIZATION=f"Bearer {new_tokens['access']}")
    assert client.get("/api/auth/me/").status_code == 200


def test_logout_requires_csrf_and_blacklists_refresh(make_user):
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

    make_user(email="logout@test.test")
    client = APIClient(enforce_csrf_checks=True)
    client.post(
        "/api/auth/login/",
        {"email": "logout@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    csrf = client.cookies["csrftoken"].value

    assert client.post("/api/auth/logout/", {}, format="json").status_code == 403
    response = client.post(
        "/api/auth/logout/", {}, format="json", HTTP_X_CSRFTOKEN=csrf
    )

    assert response.status_code == 204
    assert response.cookies["ps_refresh"]["max-age"] == 0
    assert BlacklistedToken.objects.count() == 1


def test_change_password_wrong_old_password(make_user):
    user = make_user(email="wrongold@test.test")
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.post(
        "/api/auth/change-password/",
        {
            "old_password": "WrongPassword!",
            "new_password": "N3wSecretPass123!",
            "confirm_password": "N3wSecretPass123!",
        },
        format="json",
    )
    assert resp.status_code == 400
    assert "old_password" in resp.json()


def test_change_password_mismatched_or_weak(make_user):
    user = make_user(email="mismatch@test.test")
    client = APIClient()
    client.force_authenticate(user=user)

    # Mismatched passwords
    resp = client.post(
        "/api/auth/change-password/",
        {
            "old_password": "Sup3rSecret!",
            "new_password": "N3wSecretPass123!",
            "confirm_password": "DifferentPass123!",
        },
        format="json",
    )
    assert resp.status_code == 400
    assert "confirm_password" in resp.json()

    # Weak password
    resp = client.post(
        "/api/auth/change-password/",
        {
            "old_password": "Sup3rSecret!",
            "new_password": "123",
            "confirm_password": "123",
        },
        format="json",
    )
    assert resp.status_code == 400
    assert "new_password" in resp.json()


# ----------------------------------------------------------------------
# test_closed_beta.py


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


# ----------------------------------------------------------------------
# test_account_scoping.py
# Per-account scoping of the analytics endpoint.
# 
# The top-bar portfolio selector drives `?account=<id>` on every analytics call.
# This guards the view->service boundary: that scoping resolves to the owned
# account and that `current_weights` / `total_value_tomans` reflect ONLY that
# account's holdings, while an absent param aggregates across all of the user's
# accounts. `current_weights` and `total_value_tomans` derive from the live
# valuation (not the returns matrix), so no price history is required.


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def test_analytics_scoped_to_one_account(asset_catalog, write_prices, make_user):
    write_prices({"emami_coin": Decimal("480000000"), "kama_stock": Decimal("5230")})
    pro = make_user(email="scope@t.t")

    # Two portfolios with disjoint holdings: A is all gold, B is all stock.
    a = Account.objects.create(user=pro, name="Retirement")
    b = Account.objects.create(user=pro, name="Trading")
    Holding.objects.create(account=a, asset=asset_catalog["emami_coin"], quantity=Decimal("2"))  # 960M
    Holding.objects.create(account=b, asset=asset_catalog["kama_stock"], quantity=Decimal("100"))  # 523,000

    client = _client(pro)

    # Account A: weights are 100% emami_coin, total is A's liquid value only.
    resp_a = client.get(f"/api/analytics/?account={a.id}")
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    assert set(body_a["current_weights"]) == {"emami_coin"}
    assert body_a["current_weights"]["emami_coin"] == pytest.approx(1.0)
    assert Decimal(body_a["total_value_tomans"]) == Decimal("960000000")

    # Account B: disjoint — kama_stock only, different total. Proves A did not leak in.
    resp_b = client.get(f"/api/analytics/?account={b.id}")
    assert resp_b.status_code == 200
    body_b = resp_b.json()
    assert set(body_b["current_weights"]) == {"kama_stock"}
    assert Decimal(body_b["total_value_tomans"]) == Decimal("523000")

    # No param: aggregate across both accounts.
    resp_all = client.get("/api/analytics/")
    assert resp_all.status_code == 200
    body_all = resp_all.json()
    assert set(body_all["current_weights"]) == {"emami_coin", "kama_stock"}
    assert Decimal(body_all["total_value_tomans"]) == Decimal("960523000")


def test_analytics_rejects_account_owned_by_another_user(asset_catalog, write_prices, make_user):
    write_prices({"emami_coin": Decimal("480000000"), "kama_stock": Decimal("5230")})
    pro = make_user(email="owner@t.t")
    other = make_user(email="other@t.t")

    mine = Account.objects.create(user=pro, name="Retirement")
    Holding.objects.create(account=mine, asset=asset_catalog["emami_coin"], quantity=Decimal("2"))

    theirs = Account.objects.create(user=other, name="Secret")
    Holding.objects.create(account=theirs, asset=asset_catalog["kama_stock"], quantity=Decimal("100"))

    resp = _client(pro).get(f"/api/analytics/?account={theirs.id}")
    assert resp.status_code == 404


def test_analytics_rejects_invalid_account_id(make_user):
    pro = make_user(email="invalid-scope@t.t")
    assert _client(pro).get("/api/analytics/?account=abc").status_code == 400


def test_aggregate_trend_ignores_account_snapshots(make_user):
    user = make_user(email="trend@t.t")
    account = Account.objects.create(user=user, name="Trading")
    Snapshot.objects.create(user=user, account=None, total_value_tomans=100)
    Snapshot.objects.create(user=user, account=account, total_value_tomans=1000)
    Snapshot.objects.create(user=user, account=None, total_value_tomans=110)
    assert net_worth_trend(user)["delta_pct"] == 10


def test_asset_ranking_is_scoped_to_owned_account(asset_catalog, make_user):
    pro = make_user(email="ranking@t.t")
    account = Account.objects.create(user=pro, name="Mine")
    other = Account.objects.create(user=pro, name="Other")
    asset_catalog["emami_coin"].brs_symbol = "IR_COIN_EMAMI"
    asset_catalog["emami_coin"].save(update_fields=["brs_symbol"])
    asset_catalog["kama_stock"].tse_symbol = "KAMA"
    asset_catalog["kama_stock"].save(update_fields=["tse_symbol"])
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=1
    )
    Holding.objects.create(
        account=other, asset=asset_catalog["kama_stock"], quantity=1
    )
    AssetMetricSnapshot.objects.create(
        symbol="IR_COIN_EMAMI", as_of="1405-05-10", sharpe=2
    )
    AssetMetricSnapshot.objects.create(
        symbol="KAMA", as_of="1405-05-10", sharpe=9
    )

    response = _client(pro).get(
        f"/api/analytics/asset-ranking/?account={account.id}"
    )

    assert response.status_code == 200
    assert [row["symbol"] for row in response.json()] == ["IR_COIN_EMAMI"]

"""Registration, login, JWT lifecycle, closed-beta gating, and the rule that one user never sees another's account.

Merged from 3 files; each section keeps its original banner.
"""

from datetime import timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import io
import threading

from django.core.cache import cache
from django.db import close_old_connections, connections
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


@override_settings(REGISTRATION_OPEN=False)
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


@pytest.mark.parametrize("is_open", [False, True])
def test_registration_status_matches_the_server_gate(is_open):
    with override_settings(REGISTRATION_OPEN=is_open):
        resp = APIClient().get("/api/auth/registration/")

    assert resp.status_code == 200
    # The test settings use the locmem mail backend, which counts as
    # deliverable -- mail is captured, and a developer reading a reset link out
    # of `mail.outbox` is a working flow.
    assert resp.json() == {"registration_open": is_open, "self_service_reset": True}


@pytest.mark.parametrize(
    ("backend", "host", "deliverable"),
    [
        ("django.core.mail.backends.smtp.EmailBackend", "smtp.example.com", True),
        # Django's own default host. An environment that never set EMAIL_HOST
        # looks exactly like this, which is what production was doing.
        ("django.core.mail.backends.smtp.EmailBackend", "localhost", False),
        ("django.core.mail.backends.smtp.EmailBackend", "127.0.0.1", False),
        ("django.core.mail.backends.smtp.EmailBackend", "", False),
        ("django.core.mail.backends.console.EmailBackend", "", True),
    ],
)
def test_registration_status_reports_whether_reset_mail_can_be_sent(backend, host, deliverable):
    with override_settings(EMAIL_BACKEND=backend, EMAIL_HOST=host):
        resp = APIClient().get("/api/auth/registration/")

    assert resp.status_code == 200
    assert resp.json()["self_service_reset"] is deliverable


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


def test_login_stamps_last_login_and_refresh_does_not():
    """A successful JWT obtain is a sign-in; cookie refresh is not."""
    user = User.objects.create_user(email="stamp@test.test", password="Sup3rSecret!")
    assert user.last_login is None
    client = APIClient(enforce_csrf_checks=True)
    login_resp = client.post(
        "/api/auth/login/",
        {"email": "stamp@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    user.refresh_from_db()
    stamped = user.last_login
    assert login_resp.status_code == 200
    assert stamped is not None

    csrf = client.cookies["csrftoken"].value
    client.post(
        "/api/token/refresh/", {}, format="json", HTTP_X_CSRFTOKEN=csrf
    )
    user.refresh_from_db()
    assert user.last_login == stamped

    APIClient().post(
        "/api/auth/login/",
        {"email": "stamp@test.test", "password": "wrong"},
        format="json",
    )
    user.refresh_from_db()
    assert user.last_login == stamped


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


def test_refresh_replays_its_answer_when_the_response_was_lost(make_user):
    """A rotated token presented again inside the grace window must not sign the user out.

    Rotation blacklists on use, so a refresh whose response never reached the
    browser -- a nav click cancelling the session restore, which happens on
    every page load -- left the cookie holding a token the server had already
    burned. The next request 403'd and the user landed back on the sign-in form.
    Reproduced in four navigations before the grace window existed.
    """
    make_user(email="refresh-replay@test.test")
    client = APIClient(enforce_csrf_checks=True)
    client.post(
        "/api/auth/login/",
        {"email": "refresh-replay@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    csrf = client.cookies["csrftoken"].value
    presented = client.cookies["ps_refresh"].value

    first = client.post("/api/token/refresh/", {}, format="json", HTTP_X_CSRFTOKEN=csrf)

    # The browser never committed the new cookie. A second client carrying the
    # old one models that faithfully; re-assigning it on `client` does not,
    # because the test client's jar re-applies the response cookie underneath.
    stale = APIClient(enforce_csrf_checks=True)
    stale.cookies["csrftoken"] = csrf
    stale.cookies["ps_refresh"] = presented
    replay = stale.post("/api/token/refresh/", {}, format="json", HTTP_X_CSRFTOKEN=csrf)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["access"] == first.json()["access"]
    assert replay.cookies["ps_refresh"].value == first.cookies["ps_refresh"].value


def test_refresh_token_error_during_rotation_is_auth_failure(make_user, monkeypatch):
    """A token revoked between the serializer's two reads is not a server error.

    `super().validate()` re-reads the token and raises a bare `TokenError` that
    DRF does not translate, so it escaped as a 500 whenever the token was
    blacklisted between the serializer's two reads -- exactly what a concurrent
    refresh of the same cookie does.
    """
    make_user(email="refresh-revoked@test.test")
    client = APIClient(enforce_csrf_checks=True)
    client.post(
        "/api/auth/login/",
        {"email": "refresh-revoked@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    csrf = client.cookies["csrftoken"].value

    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.serializers import TokenRefreshSerializer

    def revoked_during_rotation(_serializer, _attrs):
        raise TokenError("Token is blacklisted")

    monkeypatch.setattr(TokenRefreshSerializer, "validate", revoked_during_rotation)
    response = client.post("/api/token/refresh/", {}, format="json", HTTP_X_CSRFTOKEN=csrf)

    assert response.status_code in (401, 403)  # 403: no authenticator, so DRF downgrades
    assert response.status_code != 500


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


def test_revoking_sessions_is_one_round_trip_and_skips_expired_tokens(
    make_user, django_assert_num_queries
):
    """Refresh rotation makes this table grow forever; revocation must not walk it.

    Every refresh mints an `OutstandingToken`, so an old account holds
    thousands. The previous per-token `get_or_create` loop issued two queries
    each, inside the request that changes a password or deletes an account.
    Expired rows are skipped because the auth layer already refuses them.
    """
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken,
        OutstandingToken,
    )

    from accounts.views import _revoke_all

    user = make_user(email="bulk-revoke@test.test")
    now = timezone.now()
    OutstandingToken.objects.bulk_create(
        [
            OutstandingToken(
                user=user, jti=f"live-{i}", token=f"t-live-{i}",
                created_at=now, expires_at=now + timedelta(days=1),
            )
            for i in range(20)
        ]
        + [
            OutstandingToken(
                user=user, jti=f"dead-{i}", token=f"t-dead-{i}",
                created_at=now - timedelta(days=40), expires_at=now - timedelta(days=10),
            )
            for i in range(20)
        ]
    )

    # One SELECT for the live tokens, one bulk INSERT. Constant in the number of
    # tokens; the loop this replaced would have been 40 queries and rising.
    with django_assert_num_queries(2):
        _revoke_all(user)

    blacklisted = set(
        BlacklistedToken.objects.filter(token__user=user).values_list(
            "token__jti", flat=True
        )
    )
    assert len(blacklisted) == 20
    assert all(jti.startswith("live-") for jti in blacklisted), (
        "expired tokens were blacklisted; they are already refused on expiry"
    )

    # Idempotent: a second revoke must not raise on the rows already there.
    _revoke_all(user)
    assert BlacklistedToken.objects.filter(token__user=user).count() == 20


def test_expired_refresh_tokens_are_pruned_and_live_ones_survive(make_user):
    """Nothing else bounds these two tables; rotation writes a row per refresh."""
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken,
        OutstandingToken,
    )

    from portfolio.tasks import prune_expired_refresh_tokens

    user = make_user(email="token-prune@test.test")
    now = timezone.now()
    dead = OutstandingToken.objects.create(
        user=user, jti="dead", token="t-dead",
        created_at=now - timedelta(days=40), expires_at=now - timedelta(days=10),
    )
    OutstandingToken.objects.create(
        user=user, jti="live", token="t-live",
        created_at=now, expires_at=now + timedelta(days=1),
    )
    # A blacklist row on the expired token must go with it, or the delete fails
    # on the FK and the table stays unbounded anyway.
    BlacklistedToken.objects.create(token=dead)

    result = prune_expired_refresh_tokens()

    assert result["deleted"] >= 1
    assert list(OutstandingToken.objects.values_list("jti", flat=True)) == ["live"]
    assert BlacklistedToken.objects.count() == 0


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
    # Rial quote, Toman total: 100 shares x 5,230 Rial = 52,300 Toman.
    assert Decimal(body_b["total_value_tomans"]) == Decimal("52300")

    # No param: aggregate across both accounts.
    resp_all = client.get("/api/analytics/")
    assert resp_all.status_code == 200
    body_all = resp_all.json()
    assert set(body_all["current_weights"]) == {"emami_coin", "kama_stock"}
    assert Decimal(body_all["total_value_tomans"]) == Decimal("960052300")


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


def test_insights_returns_explainable_payload(make_user):
    user = make_user(email="insights@t.t")
    response = _client(user).get("/api/insights/")
    assert response.status_code == 200
    body = response.json()
    for key in ("valuation", "allocation", "concentration", "gold_band", "net_worth_trend"):
        assert key in body
    assert "message" in body["concentration"]


def test_insights_is_account_scoped(make_user):
    owner = make_user(email="ins-owner@t.t")
    other = make_user(email="ins-other@t.t")
    account = Account.objects.create(user=other, name="Other")
    assert _client(owner).get(f"/api/insights/?account={account.id}").status_code == 404


# ----------------------------------------------------------------------
# Password recovery. Unit tests at the auth boundary: HTTP in, mail/token
# out. Pyramid: many of these, fast, no browser.


def test_password_reset_is_silent_for_unknown_and_known_emails(make_user):
    from django.core import mail

    make_user(email="reset@test.test")
    client = APIClient()
    unknown = client.post(
        "/api/auth/password-reset/",
        {"email": "nobody@test.test"},
        format="json",
    )
    known = client.post(
        "/api/auth/password-reset/",
        {"email": "reset@test.test"},
        format="json",
    )
    assert unknown.status_code == 200
    assert known.status_code == 200
    assert unknown.json()["detail"] == known.json()["detail"]
    assert len(mail.outbox) == 1
    assert "reset@test.test" in mail.outbox[0].to
    assert "uid=" in mail.outbox[0].body
    assert "token=" in mail.outbox[0].body


def test_password_reset_confirm_rotates_password_and_revokes_sessions(make_user):
    import re
    from django.core import mail

    user = make_user(email="reset-confirm@test.test")
    from rest_framework_simplejwt.tokens import RefreshToken
    from rest_framework_simplejwt.exceptions import TokenError

    old_refresh = str(RefreshToken.for_user(user))
    client = APIClient()
    client.post(
        "/api/auth/password-reset/",
        {"email": "reset-confirm@test.test"},
        format="json",
    )
    uid, token = re.search(r"uid=([^&\s]+).*token=([^\s]+)", mail.outbox[0].body).groups()

    refused = client.post(
        "/api/auth/password-reset/confirm/",
        {
            "uid": uid,
            "token": "not-a-token",
            "new_password": "N3wSecret!!",
            "confirm_password": "N3wSecret!!",
        },
        format="json",
    )
    assert refused.status_code == 400

    ok = client.post(
        "/api/auth/password-reset/confirm/",
        {
            "uid": uid,
            "token": token,
            "new_password": "N3wSecret!!",
            "confirm_password": "N3wSecret!!",
        },
        format="json",
    )
    assert ok.status_code == 200
    user.refresh_from_db()
    assert user.check_password("N3wSecret!!")
    with pytest.raises(TokenError):
        RefreshToken(old_refresh)
    replay = client.post(
        "/api/auth/password-reset/confirm/",
        {"uid": uid, "token": token, "new_password": "AnotherSecret!42",
         "confirm_password": "AnotherSecret!42"},
        format="json",
    )
    assert replay.status_code == 400
    old = APIClient().post(
        "/api/auth/login/",
        {"email": "reset-confirm@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    fresh = APIClient().post(
        "/api/auth/login/",
        {"email": "reset-confirm@test.test", "password": "N3wSecret!!"},
        format="json",
    )
    assert old.status_code == 401
    assert fresh.status_code == 200


@pytest.mark.parametrize("bad_value", [True, 42, ["token"], {"token": "value"}])
def test_password_reset_rejects_non_string_credentials(make_user, bad_value):
    from accounts.views import RESET_TOKEN
    from django.utils.http import urlsafe_base64_encode
    from django.utils.encoding import force_bytes

    user = make_user(email="malformed-reset@test.test")
    payload = {
        "uid": urlsafe_base64_encode(force_bytes(user.pk)),
        "token": RESET_TOKEN.make_token(user),
        "new_password": "AnotherSecret!42",
        "confirm_password": "AnotherSecret!42",
    }
    for field in ("uid", "token"):
        response = APIClient().post(
            "/api/auth/password-reset/confirm/",
            {**payload, field: bad_value}, format="json",
        )
        assert response.status_code == 400
    user.refresh_from_db()
    assert user.check_password("Sup3rSecret!")


def test_password_reset_rolls_back_if_session_revocation_fails(make_user, monkeypatch):
    from accounts.views import RESET_TOKEN
    from django.utils.http import urlsafe_base64_encode
    from django.utils.encoding import force_bytes

    user = make_user(email="atomic-reset@test.test")
    token = RESET_TOKEN.make_token(user)

    def fail_revocation(user):
        raise RuntimeError("revocation unavailable")

    monkeypatch.setattr("accounts.views._revoke_all", fail_revocation)
    with pytest.raises(RuntimeError, match="revocation unavailable"):
        APIClient().post(
            "/api/auth/password-reset/confirm/",
            {"uid": urlsafe_base64_encode(force_bytes(user.pk)), "token": token,
             "new_password": "AnotherSecret!42", "confirm_password": "AnotherSecret!42"},
            format="json",
        )
    user.refresh_from_db()
    assert user.check_password("Sup3rSecret!")
    assert RESET_TOKEN.check_token(user, token)


# ----------------------------------------------------------------------
# The public surface, pinned.


# Every route reachable without a JWT, and why it has to be. Anything not on
# this list must authenticate: DRF's project-wide default is `IsAuthenticated`,
# so an endpoint becomes public only by explicitly saying `AllowAny` or
# `permission_classes = []` -- one line, easy to copy from the view above it,
# and invisible in review because it looks exactly like the intentional ones.
PUBLIC_ROUTES = {
    # Issues the CSRF cookie the cookie-refresh and logout endpoints require.
    "api/auth/csrf/",
    # Credentials in, tokens out -- the caller has no token yet by definition.
    "api/auth/login/",
    # Authenticates on the httpOnly refresh cookie plus CSRF, not on a bearer.
    "api/auth/logout/",
    "api/token/refresh/",
    # Reached by someone who cannot sign in. Answers the same 200 either way and
    # carries its own tight `password_reset` throttle scope.
    "api/auth/password-reset/",
    "api/auth/password-reset/confirm/",
    # Gated separately by settings.REGISTRATION_OPEN.
    "api/auth/register/",
    # Lets the signed-out UI mirror that server gate without duplicating it.
    "api/auth/registration/",
    # Probes. The compose healthcheck, the on-VPS watchdog and the GitHub
    # Actions probe all poll these, and none of them carries a token.
    "api/health/",
    "api/health/prices/",
    "api/health/ready/",
}


def test_only_the_named_routes_are_reachable_without_a_token():
    """A new endpoint must not become public by inheriting a copied line.

    Reads the resolver rather than a hand-kept list of views, so a route added
    under any app is covered the moment it resolves.
    """
    from django.urls import get_resolver
    from rest_framework.permissions import AllowAny

    public = set()
    total = 0

    def walk(patterns, prefix=""):
        nonlocal total
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                walk(pattern.url_patterns, prefix + str(pattern.pattern))
                continue
            view = getattr(pattern.callback, "view_class", None) or getattr(
                pattern.callback, "cls", None
            )
            if view is None:
                continue
            total += 1
            permissions = getattr(view, "permission_classes", None)
            if permissions is None:
                continue
            if not permissions or all(p is AllowAny for p in permissions):
                public.add(prefix + str(pattern.pattern))

    walk(get_resolver().url_patterns)

    assert total > 80, f"only {total} routes resolved; the walk is not seeing the API"
    assert public == PUBLIC_ROUTES, (
        "the set of routes reachable without a JWT changed. Newly public: "
        f"{sorted(public - PUBLIC_ROUTES)}. No longer public: "
        f"{sorted(PUBLIC_ROUTES - public)}. If a new one is deliberate, add it "
        "to PUBLIC_ROUTES with the reason it cannot require a token."
    )


# ---------------------------------------------------------------------------
# CROSS-USER ISOLATION. The highest-severity bug class in a multi-user financial
# app is an IDOR: an authenticated user reading or writing someone else's book
# by guessing an integer. Targeted tests already existed for trades, ledger
# reversal, snapshots and analytics -- for the paths somebody thought about. This
# sweeps *every* account-scoped route instead, and the guard below makes a new
# route join the sweep rather than quietly skip it.
#
# Every one of these paths carries `account_id` (or is the account itself), so
# ownership is decidable from the URL alone. 404 rather than 403 is the correct
# answer: telling an intruder that an account exists but is not theirs is itself
# a disclosure.
# Routes that address a specific object or perform an action: the only correct
# answer is 404. `ledger/<id>/` and `ledger/holdings/<id>/` take PATCH, not GET --
# a 405 would be returned before ownership is ever consulted and would prove
# nothing, so they are swept with the method they actually implement.
ACCOUNT_SCOPED_ROUTES = [
    ("get", "/api/accounts/{account}/"),
    ("patch", "/api/accounts/{account}/"),
    ("delete", "/api/accounts/{account}/"),
    ("get", "/api/accounts/{account}/holdings/{holding}/"),
    ("patch", "/api/accounts/{account}/holdings/{holding}/"),
    ("delete", "/api/accounts/{account}/holdings/{holding}/"),
    ("get", "/api/accounts/{account}/liabilities/{liability}/"),
    ("patch", "/api/accounts/{account}/liabilities/{liability}/"),
    ("delete", "/api/accounts/{account}/liabilities/{liability}/"),
    ("get", "/api/accounts/{account}/ledger/"),
    ("patch", "/api/accounts/{account}/ledger/holdings/{holding}/"),
    ("patch", "/api/accounts/{account}/ledger/{entry}/"),
    ("delete", "/api/accounts/{account}/ledger/{entry}/"),
    ("post", "/api/accounts/{account}/ledger/{entry}/reverse/"),
    ("post", "/api/accounts/{account}/imports/preview/"),
    ("post", "/api/accounts/{account}/imports/commit/"),
    ("get", "/api/accounts/{account}/performance/"),
    ("get", "/api/accounts/{account}/data-quality/"),
    ("get", "/api/accounts/{account}/valuation/"),
]

# List endpoints answer 200 with an empty page instead, because they filter on
# `account__user` rather than resolving the account first. That leaks nothing
# and is not an existence oracle -- the answer is the same empty page whether or
# not the id exists -- so it is asserted on content, not on status.
ACCOUNT_SCOPED_LISTS = [
    "/api/accounts/{account}/holdings/",
    "/api/accounts/{account}/liabilities/",
]

# Creation is the one that would actually move somebody else's money, and a
# rejected-for-validation 400 proves nothing about ownership: the serializer runs
# first. Each of these carries a payload valid enough to reach the view body.
ACCOUNT_SCOPED_CREATES = [
    ("/api/accounts/{account}/holdings/", {"asset_key": "emami_coin", "quantity": "1"}),
    ("/api/accounts/{account}/liabilities/", {"label": "L", "amount_tomans": "1"}),
    ("/api/accounts/{account}/trades/",
     {"asset_key": "emami_coin", "side": "buy", "quantity": "1"}),
]


@pytest.fixture
def victims_book(asset_catalog, make_user):
    """A fully populated account belonging to somebody else."""
    from portfolio.models import Account, Holding, LedgerEntry, Liability

    owner = make_user(email="owner@test.test")
    account = Account.objects.create(user=owner, name="Private")
    asset = asset_catalog["emami_coin"]
    holding = Holding.objects.create(
        account=account, asset=asset, quantity=Decimal("3")
    )
    liability = Liability.objects.create(
        account=account, label="Loan", amount_tomans=Decimal("1000")
    )
    entry = LedgerEntry.objects.create(
        account=account, asset=asset, kind=LedgerEntry.Kind.BUY,
        quantity=Decimal("1"), price_tomans=Decimal("100"),
    )
    return {
        "account": account.id, "holding": holding.id,
        "liability": liability.id, "entry": entry.id,
    }


@pytest.mark.parametrize("method,template", ACCOUNT_SCOPED_ROUTES)
def test_a_stranger_cannot_touch_another_users_account(
    method, template, victims_book, make_user
):
    from rest_framework.test import APIClient

    intruder = make_user(email="intruder@test.test")
    client = APIClient()
    client.force_authenticate(user=intruder)

    url = template.format(**victims_book)
    response = getattr(client, method)(url, {}, format="json")

    assert response.status_code == 404, (
        f"{method.upper()} {url} answered {response.status_code}; an account-scoped "
        f"route must not acknowledge another user's object"
    )


@pytest.mark.parametrize("template", ACCOUNT_SCOPED_LISTS)
def test_a_stranger_sees_an_empty_page_not_another_users_rows(
    template, victims_book, make_user
):
    from rest_framework.test import APIClient

    intruder = make_user(email="intruder@test.test")
    client = APIClient()
    client.force_authenticate(user=intruder)

    response = client.get(template.format(**victims_book))

    assert response.status_code == 200
    rows = response.data["results"] if isinstance(response.data, dict) else response.data
    assert rows == [], f"{template} returned another user's rows: {rows}"


@pytest.mark.parametrize("template,payload", ACCOUNT_SCOPED_CREATES)
def test_a_stranger_cannot_create_inside_another_users_account(
    template, payload, victims_book, asset_catalog, make_user
):
    from portfolio.models import Holding, LedgerEntry, Liability
    from rest_framework.test import APIClient

    intruder = make_user(email="intruder@test.test")
    client = APIClient()
    client.force_authenticate(user=intruder)
    before = (
        Holding.objects.count(),
        Liability.objects.count(),
        LedgerEntry.objects.count(),
    )

    response = client.post(template.format(**victims_book), payload, format="json")

    assert response.status_code == 404, (
        f"POST {template} answered {response.status_code} for a foreign account"
    )
    assert (
        Holding.objects.count(),
        Liability.objects.count(),
        LedgerEntry.objects.count(),
    ) == before, "a write reached another user's account"


def test_every_account_scoped_route_is_in_the_isolation_sweep():
    """A new route under /accounts/<id>/ must join the sweep above.

    Without this, adding an endpoint that forgets to scope its queryset ships
    green: the sweep only proves things about the routes it happens to list.
    """
    import re

    from portfolio import urls as portfolio_urls

    declared = {
        str(pattern.pattern)
        for pattern in portfolio_urls.urlpatterns
        if re.match(r"^accounts/<int:(pk|account_id)>/", str(pattern.pattern))
    }
    swept = (
        [template for _method, template in ACCOUNT_SCOPED_ROUTES]
        + list(ACCOUNT_SCOPED_LISTS)
        + [template for template, _payload in ACCOUNT_SCOPED_CREATES]
    )
    covered = {
        re.sub(r"\{[a-z_]+\}", "PARAM", template.removeprefix("/api/"))
        for template in swept
    }
    missing = {
        route for route in declared
        if re.sub(r"<int:[a-z_]+>", "PARAM", route) not in covered
    }
    assert not missing, f"account-scoped routes not covered by the sweep: {missing}"


# ---------------------------------------------------------------------------
# OPERATOR-ISSUED RESET LINK. Password reset is mint-a-token plus deliver-it, and
# only delivery is broken in production: there is no mail relay, so the request
# endpoint answers 200 and sends nothing. These pin the working half being
# exposed to superusers, and -- more importantly -- nobody else.
class TestAdminPasswordResetLink:
    URL = "/api/auth/admin/password-reset-link/"

    def _client(self, user=None):
        from rest_framework.test import APIClient

        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client

    def test_anonymous_ordinary_and_staff_users_are_refused(self, make_user):
        from accounts.models import User

        ordinary = make_user(email="ordinary@test.test")
        staff = make_user(email="staff@test.test")
        User.objects.filter(pk=staff.pk).update(is_staff=True)
        staff.refresh_from_db()
        target = make_user(email="owner@test.test")
        User.objects.filter(pk=target.pk).update(is_staff=True, is_superuser=True)
        target.refresh_from_db()

        anon = self._client().post(self.URL, {"email": ordinary.email}, format="json")
        theirs = self._client(ordinary).post(
            self.URL, {"email": ordinary.email}, format="json"
        )
        staff_response = self._client(staff).post(
            self.URL, {"email": target.email}, format="json"
        )

        assert anon.status_code in (401, 403)
        # Not even for their own address: this is an operator tool, and a
        # self-service route that bypasses delivery would be a way to mint a
        # reset link for any address the throttle would otherwise slow down.
        assert theirs.status_code == 403
        assert staff_response.status_code == 403

    def test_a_superuser_link_actually_resets_the_password(self, make_user):
        from accounts.models import User

        staff = make_user(email="staff@test.test")
        User.objects.filter(pk=staff.pk).update(is_staff=True, is_superuser=True)
        staff.refresh_from_db()
        forgetful = make_user(email="forgetful@test.test")

        issued = self._client(staff).post(
            self.URL, {"email": "Forgetful@Test.Test"}, format="json"
        )
        assert issued.status_code == 200

        query = issued.data["link"].split("?", 1)[1]
        params = dict(pair.split("=", 1) for pair in query.split("&"))
        confirmed = self._client().post(
            "/api/auth/password-reset/confirm/",
            {**params, "new_password": "An0therSecret!", "confirm_password": "An0therSecret!"},
            format="json",
        )

        assert confirmed.status_code == 200, confirmed.data
        forgetful.refresh_from_db()
        assert forgetful.check_password("An0therSecret!")

    def test_the_link_is_single_use(self, make_user):
        """Redeeming it invalidates it: the token hashes the password it replaced."""
        from accounts.models import User

        staff = make_user(email="staff@test.test")
        User.objects.filter(pk=staff.pk).update(is_staff=True, is_superuser=True)
        staff.refresh_from_db()
        make_user(email="forgetful@test.test")

        issued = self._client(staff).post(
            self.URL, {"email": "forgetful@test.test"}, format="json"
        )
        query = issued.data["link"].split("?", 1)[1]
        params = dict(pair.split("=", 1) for pair in query.split("&"))
        payload = {**params, "new_password": "An0therSecret!", "confirm_password": "An0therSecret!"}

        first = self._client().post("/api/auth/password-reset/confirm/", payload, format="json")
        second = self._client().post("/api/auth/password-reset/confirm/", payload, format="json")

        assert first.status_code == 200
        assert second.status_code == 400

    def test_an_unknown_address_gets_no_link(self, make_user):
        from accounts.models import User

        staff = make_user(email="staff@test.test")
        User.objects.filter(pk=staff.pk).update(is_staff=True, is_superuser=True)
        staff.refresh_from_db()

        response = self._client(staff).post(
            self.URL, {"email": "nobody@test.test"}, format="json"
        )

        assert response.status_code == 404
        assert "link" not in response.data


# ----------------------------------------------------------------------
# Member administration. `is_active` is the entire ban mechanism in this
# product, so these cover the lever itself and the two ways using it could
# lock the installation out of its own admin.


def _staff(make_user, email, superuser=False):
    user = make_user(email=email)
    User.objects.filter(pk=user.pk).update(is_staff=True, is_superuser=superuser)
    user.refresh_from_db()
    return user


def test_member_list_is_staff_only_and_reports_status_and_portfolios(make_user):
    staff = _staff(make_user, "roster-staff@test.test")
    member = make_user(email="roster-member@test.test")
    Account.objects.create(user=member, name="Main")

    assert _client(member).get("/api/auth/admin/users/").status_code == 403

    rows = {row["email"]: row for row in _client(staff).get("/api/auth/admin/users/").json()}
    assert rows["roster-member@test.test"]["is_active"] is True
    assert rows["roster-member@test.test"]["accounts_count"] == 1
    assert rows["roster-staff@test.test"]["accounts_count"] == 0


def test_member_list_filters_by_active_flag(make_user):
    staff = _staff(make_user, "filter-staff@test.test")
    banned = make_user(email="filter-banned@test.test")
    User.objects.filter(pk=banned.pk).update(is_active=False)

    active = _client(staff).get("/api/auth/admin/users/?active=1").json()
    inactive = _client(staff).get("/api/auth/admin/users/?active=0").json()

    assert banned.email not in [row["email"] for row in active]
    assert [row["email"] for row in inactive] == [banned.email]


def test_deactivating_a_member_revokes_their_live_refresh_tokens(make_user):
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
    from rest_framework_simplejwt.tokens import RefreshToken

    staff = _staff(make_user, "ban-staff@test.test")
    member = make_user(email="ban-member@test.test")
    refresh = RefreshToken.for_user(member)

    resp = _client(staff).patch(
        f"/api/auth/admin/users/{member.pk}/", {"is_active": False}, format="json"
    )

    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    member.refresh_from_db()
    assert member.is_active is False
    # The point of the revoke: without it the member's refresh token keeps
    # minting access tokens for its full lifetime and the ban does not take.
    assert BlacklistedToken.objects.filter(token__jti=refresh["jti"]).exists()


def test_a_deactivated_member_can_neither_log_in_nor_refresh(make_user):
    from rest_framework_simplejwt.tokens import RefreshToken

    member = make_user(email="inactive@test.test")
    refresh = str(RefreshToken.for_user(member))
    User.objects.filter(pk=member.pk).update(is_active=False)

    client = APIClient()
    login = client.post(
        "/api/auth/login/",
        {"email": "inactive@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    client.cookies["ps_refresh"] = refresh
    refreshed = client.post("/api/token/refresh/", {}, format="json")

    assert login.status_code == 401
    # This endpoint deliberately has no authenticator because the refresh
    # credential is an HttpOnly cookie; DRF may render AuthenticationFailed as
    # 403 when it cannot emit a WWW-Authenticate challenge.
    assert refreshed.status_code in (401, 403)


def test_an_operator_cannot_deactivate_their_own_account(make_user):
    staff = _staff(make_user, "self-ban@test.test")

    resp = _client(staff).patch(
        f"/api/auth/admin/users/{staff.pk}/", {"is_active": False}, format="json"
    )

    assert resp.status_code == 400
    assert "your own account" in resp.json()["detail"]
    staff.refresh_from_db()
    assert staff.is_active is True


def test_the_last_active_superuser_cannot_be_deactivated(make_user):
    """Otherwise the installation can be locked out of its own admin.

    Two superusers here, and the requester is a third party (plain staff), so
    the self-deactivation guard cannot be what refuses the last one -- this
    isolates the superuser-count guard.
    """
    staff = _staff(make_user, "count-staff@test.test")
    first_root = _staff(make_user, "op-one@test.test", superuser=True)
    second_root = _staff(make_user, "op-two@test.test", superuser=True)

    # Two active superusers: removing one is allowed.
    allowed = _client(staff).patch(
        f"/api/auth/admin/users/{second_root.pk}/", {"is_active": False}, format="json"
    )
    # Only `first_root` is left, so now the same call is refused.
    refused = _client(staff).patch(
        f"/api/auth/admin/users/{first_root.pk}/", {"is_active": False}, format="json"
    )
    # Reinstating is never refused.
    reinstated = _client(staff).patch(
        f"/api/auth/admin/users/{second_root.pk}/", {"is_active": True}, format="json"
    )

    assert allowed.status_code == 200
    assert refused.status_code == 400
    assert "last active superuser" in refused.json()["detail"]
    assert reinstated.status_code == 200
    first_root.refresh_from_db()
    assert first_root.is_active is True


@pytest.mark.django_db(transaction=True)
def test_concurrent_deactivations_leave_one_active_superuser(make_user, monkeypatch):
    """Integration: concurrent staff requests must serialize the root-count check."""
    from accounts.serializers import AdminUserSerializer

    staff = _staff(make_user, "race-staff@test.test")
    first_root = _staff(make_user, "race-one@test.test", superuser=True)
    second_root = _staff(make_user, "race-two@test.test", superuser=True)
    original_save = AdminUserSerializer.save
    both_requests_started = threading.Barrier(2)
    first_save = threading.Event()
    second_save = threading.Event()
    arrivals = 0
    arrivals_lock = threading.Lock()

    def delayed_save(serializer, **kwargs):
        nonlocal arrivals
        with arrivals_lock:
            arrivals += 1
            if arrivals == 1:
                first_save.set()
            else:
                second_save.set()
        # Before the lock fix both requests reach save and continue together.
        # With it, request two cannot reach save until request one commits.
        second_save.wait(timeout=0.5)
        return original_save(serializer, **kwargs)

    monkeypatch.setattr(AdminUserSerializer, "save", delayed_save)

    def deactivate(user):
        close_old_connections()
        try:
            both_requests_started.wait(timeout=3)
            client = APIClient()
            client.force_authenticate(staff)
            return client.patch(
                f"/api/auth/admin/users/{user.pk}/", {"is_active": False}, format="json"
            ).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(deactivate, (first_root, second_root)))

    assert sorted(results) == [200, 400]
    assert User.objects.filter(is_superuser=True, is_active=True).count() == 1


def test_reinstating_a_member_restores_login(make_user):
    staff = _staff(make_user, "reinstate-staff@test.test")
    member = make_user(email="reinstate-member@test.test")
    User.objects.filter(pk=member.pk).update(is_active=False)

    resp = _client(staff).patch(
        f"/api/auth/admin/users/{member.pk}/", {"is_active": True}, format="json"
    )
    login = APIClient().post(
        "/api/auth/login/",
        {"email": "reinstate-member@test.test", "password": "Sup3rSecret!"},
        format="json",
    )

    assert resp.status_code == 200
    assert login.status_code == 200


def test_member_patch_requires_the_is_active_field(make_user):
    staff = _staff(make_user, "patch-staff@test.test")
    member = make_user(email="patch-member@test.test")

    resp = _client(staff).patch(
        f"/api/auth/admin/users/{member.pk}/", {"is_staff": True}, format="json"
    )

    assert resp.status_code == 400
    member.refresh_from_db()
    assert member.is_staff is False


def test_missing_member_is_a_404(make_user):
    staff = _staff(make_user, "missing-staff@test.test")

    resp = _client(staff).patch(
        "/api/auth/admin/users/999999/", {"is_active": False}, format="json"
    )

    assert resp.status_code == 404


# ----------------------------------------------------------------------
# Self-service reset when there is no relay. The endpoint must answer
# identically either way -- a different answer enumerates accounts -- but it
# must not spend EMAIL_TIMEOUT seconds proving a dead host is dead.


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST="localhost"
)
def test_password_reset_sends_nothing_when_mail_is_not_deliverable(make_user, monkeypatch):
    from django.core import mail

    make_user(email="no-relay@test.test")
    attempted = []
    monkeypatch.setattr(
        "accounts.views._send_reset_mail", lambda user: attempted.append(user)
    )
    client = APIClient()

    known = client.post(
        "/api/auth/password-reset/", {"email": "no-relay@test.test"}, format="json"
    )
    unknown = client.post(
        "/api/auth/password-reset/", {"email": "nobody@test.test"}, format="json"
    )

    assert known.status_code == 200
    assert known.json()["detail"] == unknown.json()["detail"]
    assert attempted == []
    assert mail.outbox == []

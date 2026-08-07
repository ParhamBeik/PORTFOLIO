"""Authentication: register, login, JWT-protected /me/."""
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_auth_throttles():
    cache.clear()


def test_register_logs_in_immediately_and_sends_verification_email():
    """Signup is minimal-friction: no first/last name, and no wait for the
    verification email before the user can use the app."""
    from django.core import mail

    client = APIClient()
    resp = client.post(
        "/api/auth/register/",
        {"email": "new@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["user"]["email"] == "new@test.test"
    assert data["user"]["tier"] == "FREE"
    assert "access" in data
    assert "session_expires_at" in data
    assert resp.cookies["ps_refresh"]["httponly"] is True

    from accounts.models import User
    user = User.objects.get(email="new@test.test")
    assert user.is_active is True
    assert user.email_verified_at is None
    assert len(mail.outbox) == 1

    # /me/ works right away with the token from registration.
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {data['access']}")
    assert client.get("/api/auth/me/").status_code == 200


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


def test_me_exposes_pro_expiry(make_user):
    user = make_user(tier="PRO")
    user.pro_expires_at = timezone.now() + timedelta(days=30)
    user.save(update_fields=["pro_expires_at"])
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get("/api/auth/me/")

    assert response.status_code == 200
    assert response.json()["pro_expires_at"] is not None


def test_register_rejects_weak_all_numeric_password():
    """AUTH_PASSWORD_VALIDATORS must apply on register (previously bypassed)."""
    resp = APIClient().post(
        "/api/auth/register/",
        {"email": "weak@test.test", "password": "12345678"},
        format="json",
    )
    assert resp.status_code == 400
    assert "password" in resp.json()

    from accounts.models import User
    assert not User.objects.filter(email="weak@test.test").exists()


def test_register_rejects_too_short_password():
    resp = APIClient().post(
        "/api/auth/register/",
        {"email": "short@test.test", "password": "Ab1!"},
        format="json",
    )
    assert resp.status_code == 400
    assert "password" in resp.json()


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


def test_change_password_and_export_require_verified_email():
    """Signup no longer blocks login on verification, but sensitive actions
    (password change, data export) still require a verified email."""
    from accounts.models import User

    user = User.objects.create_user(
        email="unverified@test.test", password="Sup3rSecret!", email_verified_at=None
    )
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
    assert resp.status_code == 403
    assert client.get("/api/auth/export/").status_code == 403

"""Authentication: register, login, JWT-protected /me/."""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_register_returns_user_and_tokens():
    client = APIClient()
    resp = client.post(
        "/api/auth/register/",
        {"email": "new@test.test", "password": "Sup3rSecret!", "first_name": "New"},
        format="json",
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["user"]["email"] == "new@test.test"
    assert data["user"]["tier"] == "FREE"
    assert "access" in data and "refresh" in data


def test_login_returns_tokens():
    from accounts.models import User

    User.objects.create_user(email="login@test.test", password="Sup3rSecret!")
    resp = APIClient().post(
        "/api/auth/login/",
        {"email": "login@test.test", "password": "Sup3rSecret!"},
        format="json",
    )
    assert resp.status_code == 200
    assert "access" in resp.json()


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
    make_user(email="revoke@test.test")
    client = APIClient()
    old_tokens = client.post(
        "/api/auth/login/",
        {"email": "revoke@test.test", "password": "Sup3rSecret!"},
        format="json",
    ).json()
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
    assert APIClient().post(
        "/api/token/refresh/",
        {"refresh": old_tokens["refresh"]},
        format="json",
    ).status_code == 401

    client.credentials(HTTP_AUTHORIZATION=f"Bearer {new_tokens['access']}")
    assert client.get("/api/auth/me/").status_code == 200
    assert APIClient().post(
        "/api/token/refresh/",
        {"refresh": new_tokens["refresh"]},
        format="json",
    ).status_code == 200


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

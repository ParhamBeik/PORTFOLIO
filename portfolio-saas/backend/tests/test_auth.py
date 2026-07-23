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

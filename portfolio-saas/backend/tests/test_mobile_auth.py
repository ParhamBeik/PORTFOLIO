"""Native sessions return refresh tokens only to the Capacitor origin."""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import User

pytestmark = pytest.mark.django_db
MOBILE_ORIGIN = "capacitor://localhost"


@pytest.fixture(autouse=True)
def clear_auth_cache():
    cache.clear()


def mobile_client():
    return APIClient(HTTP_ORIGIN=MOBILE_ORIGIN, enforce_csrf_checks=True)


def test_mobile_login_rotate_replay_and_logout():
    user = User.objects.create_user(email="mobile@test.test", password="Sup3rSecret!")
    client = mobile_client()
    login = client.post("/api/auth/mobile/login/", {
        "email": "mobile@test.test", "password": "Sup3rSecret!"
    }, format="json")
    assert login.status_code == 200
    first = login.json()["refresh"]
    assert login.json()["user"]["id"] == user.id
    assert "access" in login.json() and "session_expires_at" in login.json()
    assert "ps_refresh" not in login.cookies

    rotated = client.post("/api/auth/mobile/refresh/", {"refresh": first}, format="json")
    assert rotated.status_code == 200
    second = rotated.json()["refresh"]
    assert second != first
    replay = client.post("/api/auth/mobile/refresh/", {"refresh": first}, format="json")
    assert replay.status_code == 200
    assert replay.json()["refresh"] == second

    assert client.post("/api/auth/mobile/logout/", {"refresh": second}, format="json").status_code == 204
    denied = client.post("/api/auth/mobile/refresh/", {"refresh": second}, format="json")
    assert denied.status_code == 401, denied.json()
    denied = client.post("/api/auth/mobile/refresh/", {"refresh": first}, format="json")
    assert denied.status_code == 401, denied.json()


def test_mobile_tokens_are_not_returned_to_web_origin():
    User.objects.create_user(email="web@test.test", password="Sup3rSecret!")
    response = APIClient(HTTP_ORIGIN="https://portfolio.parhambm.ir").post(
        "/api/auth/mobile/login/", {"email": "web@test.test", "password": "Sup3rSecret!"}, format="json"
    )
    assert response.status_code == 403
    assert "refresh" not in response.json()


def test_mobile_registration_and_password_change_revoke_old_session():
    client = mobile_client()
    registered = client.post("/api/auth/mobile/register/", {
        "email": "newmobile@test.test", "password": "Sup3rSecret!"
    }, format="json")
    assert registered.status_code == 201
    old = registered.json()["refresh"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {registered.json()['access']}")
    changed = client.post("/api/auth/mobile/change-password/", {
        "old_password": "Sup3rSecret!",
        "new_password": "An0therSecret!",
        "confirm_password": "An0therSecret!",
    }, format="json")
    assert changed.status_code == 200
    assert changed.json()["refresh"] != old
    denied = client.post("/api/auth/mobile/refresh/", {"refresh": old}, format="json")
    assert denied.status_code == 401, denied.json()


def test_mobile_logout_all_revokes_all_refresh_tokens():
    User.objects.create_user(email="both@test.test", password="Sup3rSecret!")
    client = APIClient(HTTP_ORIGIN="http://localhost")
    tokens = []
    for _ in range(2):
        response = client.post("/api/auth/mobile/login/", {
            "email": "both@test.test", "password": "Sup3rSecret!"
        }, format="json")
        assert response.status_code == 200
        tokens.append(response.json()["refresh"])
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")
    assert client.post("/api/auth/mobile/logout-all/", {}, format="json").status_code == 204
    for token in tokens:
        client.credentials()
        denied = client.post("/api/auth/mobile/refresh/", {"refresh": token}, format="json")
        assert denied.status_code == 401, denied.json()

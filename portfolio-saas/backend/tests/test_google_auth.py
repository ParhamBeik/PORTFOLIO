"""Google Identity Services sign-in: verify-token, match-or-create, issue JWTs."""
from unittest import mock

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import User

pytestmark = pytest.mark.django_db

CLIENT_ID = "test-client-id.apps.googleusercontent.com"


def _claims(**overrides):
    claims = {
        "sub": "google-sub-123",
        "email": "googleuser@test.test",
        "email_verified": True,
        "given_name": "Goog",
        "family_name": "User",
    }
    claims.update(overrides)
    return claims


@override_settings(GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID)
def test_missing_client_id_returns_503(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""
    resp = APIClient().post("/api/auth/google/", {"credential": "whatever"}, format="json")
    assert resp.status_code == 503


@override_settings(GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID)
def test_unverified_email_rejected():
    with mock.patch(
        "google.oauth2.id_token.verify_oauth2_token",
        return_value=_claims(email_verified=False),
    ):
        resp = APIClient().post("/api/auth/google/", {"credential": "tok"}, format="json")
    assert resp.status_code == 400
    assert not User.objects.filter(email="googleuser@test.test").exists()


@override_settings(GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID)
def test_invalid_credential_rejected():
    with mock.patch(
        "google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("bad token")
    ):
        resp = APIClient().post("/api/auth/google/", {"credential": "tok"}, format="json")
    assert resp.status_code == 400


@override_settings(GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID)
def test_new_user_created_with_unusable_password_and_verified_email():
    with mock.patch(
        "google.oauth2.id_token.verify_oauth2_token", return_value=_claims()
    ):
        resp = APIClient().post("/api/auth/google/", {"credential": "tok"}, format="json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["email"] == "googleuser@test.test"
    assert "access" in data
    assert resp.cookies["ps_refresh"]["httponly"] is True

    user = User.objects.get(email="googleuser@test.test")
    assert user.google_sub == "google-sub-123"
    assert user.email_verified_at is not None
    assert user.has_usable_password() is False


@override_settings(GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID)
def test_existing_user_matched_by_email_then_bound_by_sub(make_user):
    user = make_user(email="googleuser@test.test")
    assert user.google_sub == ""

    with mock.patch(
        "google.oauth2.id_token.verify_oauth2_token", return_value=_claims()
    ):
        resp = APIClient().post("/api/auth/google/", {"credential": "tok"}, format="json")
    assert resp.status_code == 200

    user.refresh_from_db()
    assert user.google_sub == "google-sub-123"
    # Existing password login must still work — Google sign-in only links the account.
    assert user.has_usable_password() is True

    # Second sign-in matches by google_sub even if the Google email later changes.
    with mock.patch(
        "google.oauth2.id_token.verify_oauth2_token",
        return_value=_claims(email="new-address@test.test"),
    ):
        resp2 = APIClient().post("/api/auth/google/", {"credential": "tok2"}, format="json")
    assert resp2.status_code == 200
    assert resp2.json()["user"]["email"] == "googleuser@test.test"
    assert User.objects.filter(email="googleuser@test.test").count() == 1

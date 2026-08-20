from unittest import mock

import pytest
from django.test import override_settings
from rest_framework.test import APIClient


pytestmark = pytest.mark.django_db


def test_request_id_is_accepted_or_generated():
    accepted = APIClient().get(
        "/api/health/", HTTP_X_REQUEST_ID="beta-request-123"
    )
    generated = APIClient().get("/api/health/")

    assert accepted["X-Request-ID"] == "beta-request-123"
    assert generated["X-Request-ID"]
    assert generated["X-Request-ID"] != accepted["X-Request-ID"]


@override_settings(ALERT_WEBHOOK_URL="https://alerts.test/hook")
def test_alert_notifier_redacts_and_deduplicates():
    from config.observability import notify

    with mock.patch("config.observability.requests.post") as post:
        first = notify(
            "test-alert",
            {"password": "secret", "nested": {"authorization": "Bearer token"}},
            dedupe_seconds=60,
        )
        second = notify(
            "test-alert",
            {"password": "secret", "nested": {"authorization": "Bearer token"}},
            dedupe_seconds=60,
        )

    assert first is True
    assert second is False
    payload = post.call_args.kwargs["json"]["details"]
    assert payload["password"] == "[redacted]"
    assert payload["nested"]["authorization"] == "[redacted]"
    assert post.call_args.kwargs["timeout"] == 5


def test_sentry_disabled_does_not_import_or_send():
    from config.observability import init_sentry

    with mock.patch.dict("sys.modules", {"sentry_sdk": None}):
        assert init_sentry("") is False

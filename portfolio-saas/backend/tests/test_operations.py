from datetime import timedelta
from unittest import mock

import pytest
import requests
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from billing.models import Payment


pytestmark = pytest.mark.django_db


def _payment(user, authority, *, age_minutes=20, status=Payment.Status.PENDING):
    payment = Payment.objects.create(
        user=user,
        authority=authority,
        amount_rial=10_000,
        status=status,
    )
    Payment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(minutes=age_minutes)
    )
    payment.refresh_from_db()
    return payment


def test_payment_history_is_account_scoped(make_user):
    user = make_user(email="history@test.test")
    other = make_user(email="other-history@test.test")
    _payment(user, "OWN")
    _payment(other, "OTHER")
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get("/api/billing/payments/")

    assert response.status_code == 200
    assert [row["authority"] for row in response.json()["payments"]] == ["OWN"]
    assert response.json()["is_pro"] is False


def test_reconcile_pending_payments_handles_success_failure_and_transient(make_user):
    from billing.tasks import reconcile_pending_payments

    user = make_user(email="reconcile@test.test")
    success = _payment(user, "SUCCESS")
    failed = _payment(user, "FAILED")
    transient = _payment(user, "TRANSIENT")

    def verify(*, authority, amount_rial):
        if authority == success.authority:
            return True, "ref-101", 101
        if authority == failed.authority:
            return False, "", -54
        raise requests.ConnectionError("temporary")

    with mock.patch("billing.tasks.verify_payment", side_effect=verify):
        result = reconcile_pending_payments()

    success.refresh_from_db()
    failed.refresh_from_db()
    transient.refresh_from_db()
    assert result == {"verified": 1, "failed": 1, "pending": 1, "repaired": 0}
    assert success.status == Payment.Status.VERIFIED
    assert failed.status == Payment.Status.FAILED
    assert transient.status == Payment.Status.PENDING


def test_reconcile_repairs_paid_user_and_alerts(make_user):
    from billing.tasks import reconcile_pending_payments

    user = make_user(email="repair@test.test", tier=User.Tier.FREE)
    payment = _payment(user, "REPAIR", status=Payment.Status.VERIFIED)
    Payment.objects.filter(pk=payment.pk).update(verified_at=timezone.now())

    with mock.patch("billing.tasks.notify") as notify:
        result = reconcile_pending_payments()

    user.refresh_from_db()
    assert result["repaired"] == 1
    assert user.is_pro()
    notify.assert_any_call(
        "paid-but-not-activated",
        mock.ANY,
        dedupe_seconds=3600,
    )


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
    from config.alerts import notify

    with mock.patch("config.alerts.requests.post") as post:
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

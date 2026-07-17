"""Stripe billing: Checkout creation + signature-verified, idempotent webhook.

The Stripe SDK calls are mocked: we assert the request shape and the tier flips,
not Stripe's network behaviour. `apply_subscription_event` is exercised through
the real HTTP endpoint so the idempotency transaction is covered end-to-end.
"""
import json
from unittest import mock

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from billing.models import WebhookEvent


pytestmark = pytest.mark.django_db


# ----- Checkout --------------------------------------------------------------


def test_checkout_creates_session_and_returns_url(make_user):
    user = make_user(tier=User.Tier.FREE)
    client = APIClient()
    client.force_authenticate(user=user)

    fake = mock.Mock()
    fake.url = "https://checkout.stripe.com/c/cs_test_123"
    with mock.patch("billing.views.stripe.checkout.Session.create", return_value=fake) as m:
        resp = client.post("/api/billing/checkout/")

    assert resp.status_code == 200
    assert resp.json()["url"] == "https://checkout.stripe.com/c/cs_test_123"
    # The session pins the user id so the webhook can resolve them later.
    _, kwargs = m.call_args
    assert kwargs["mode"] == "subscription"
    assert kwargs["client_reference_id"] == str(user.id)
    assert kwargs["line_items"] == [{"price": ""}]  # empty default in tests


def test_checkout_requires_auth():
    client = APIClient()
    assert client.post("/api/billing/checkout/").status_code == 401


# ----- Webhook: happy paths --------------------------------------------------


def _post_event(client, event):
    """POST a (already-validated) event to the webhook endpoint."""
    with mock.patch("billing.views.stripe.Webhook.construct_event", return_value=event):
        return client.post(
            "/api/billing/webhook/",
            data=json.dumps(event),
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=fakesig",
        )


def test_webhook_upgrades_on_checkout_completed(make_user):
    user = make_user(tier=User.Tier.FREE)
    client = APIClient()

    event = {
        "id": "evt_completed_1",
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(user.id)}},
    }
    assert _post_event(client, event).status_code == 200

    user.refresh_from_db()
    assert user.tier == User.Tier.PRO


def test_webhook_upgrades_on_invoice_paid_and_sets_customer(make_user):
    user = make_user(tier=User.Tier.FREE)
    client = APIClient()

    event = {
        "id": "evt_invoice_1",
        "type": "invoice.paid",
        "data": {"object": {"customer": "cus_abc"}},
    }
    # Resolve by customer id only after we stamp it on the user (second delivery).
    _post_event(client, event)  # first time: no match (no customer on user yet) -> still 200

    # Pretend the checkout already linked the customer to the user.
    user.customer_id = "cus_abc"
    user.save(update_fields=["customer_id"])

    event2 = {**event, "id": "evt_invoice_2"}
    assert _post_event(client, event2).status_code == 200
    user.refresh_from_db()
    assert user.tier == User.Tier.PRO


def test_webhook_downgrades_on_subscription_deleted(make_user):
    user = make_user(tier=User.Tier.PRO, email="pro@test.test")
    user.customer_id = "cus_xyz"
    user.save(update_fields=["customer_id"])
    client = APIClient()

    event = {
        "id": "evt_deleted_1",
        "type": "customer.subscription.deleted",
        "data": {"object": {"customer": "cus_xyz"}},
    }
    assert _post_event(client, event).status_code == 200

    user.refresh_from_db()
    assert user.tier == User.Tier.FREE


# ----- Webhook: idempotency + error paths -----------------------------------


def test_webhook_is_idempotent_on_replay(make_user):
    user = make_user(tier=User.Tier.FREE)
    client = APIClient()
    event = {
        "id": "evt_replay",
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(user.id)}},
    }

    first = _post_event(client, event)
    second = _post_event(client, event)  # Stripe retry: same event id
    assert first.status_code == second.status_code == 200

    assert WebhookEvent.objects.filter(event_id="evt_replay").count() == 1
    user.refresh_from_db()
    assert user.tier == User.Tier.PRO


def test_webhook_bad_signature_returns_400(make_user):
    import stripe

    client = APIClient()
    with mock.patch(
        "billing.views.stripe.Webhook.construct_event",
        side_effect=stripe.error.SignatureVerificationError("bad sig", "t=1,v1=x"),
    ):
        resp = client.post(
            "/api/billing/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=x",
        )
    assert resp.status_code == 400
    assert WebhookEvent.objects.count() == 0  # nothing recorded on a bad signature


def test_webhook_unhandled_event_recorded_without_tier_change(make_user):
    user = make_user(tier=User.Tier.FREE)
    client = APIClient()
    event = {
        "id": "evt_refund",
        "type": "charge.refunded",
        "data": {"object": {"customer": "cus_who"}},
    }
    assert _post_event(client, event).status_code == 200
    # Recorded for auditability, but no tier flip and no user matched.
    assert WebhookEvent.objects.filter(event_id="evt_refund", type="charge.refunded").exists()
    user.refresh_from_db()
    assert user.tier == User.Tier.FREE

"""Zarinpal billing: authority request + browser-callback verify, idempotent.

The gateway HTTP calls are mocked (`requests.post`), so we assert our own
request shape, the Rial amount, the tier flip + expiry, and idempotency — not
Zarinpal's network. The full request→pay→callback→verify→PRO path runs through
the real endpoints so the Payment state machine is covered end-to-end.
"""
from unittest import mock
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from billing.models import Payment


pytestmark = pytest.mark.django_db


# ----- helpers ---------------------------------------------------------------


def _fake_resp(payload, status_code=200):
    """A stand-in `requests.Response` with a fixed JSON body."""
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


REQUEST_OK = _fake_resp({"data": {"code": 100, "authority": "A000001"}})
VERIFY_100 = _fake_resp({"data": {"code": 100, "ref_id": "123456789"}})
VERIFY_101 = _fake_resp({"data": {"code": 101}})  # already verified (idempotent)
VERIFY_FAILED = _fake_resp({"data": {"code": -54}})  # unpaid / not found


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _seed_pending(user, authority="A000001"):
    """The Payment row the request step would have created for a given authority."""
    return Payment.objects.create(
        user=user, authority=authority, amount_rial=10_000_000,
        status=Payment.Status.PENDING,
    )


# ----- 1. request creates a pending Payment + redirect URL -------------------


def test_request_creates_pending_payment_and_redirect_url(make_user):
    user = make_user(tier=User.Tier.FREE)
    with mock.patch("billing.zarinpal.requests.post", return_value=REQUEST_OK) as m:
        resp = _client(user).post("/api/billing/zarinpal/request/")

    assert resp.status_code == 200
    assert resp.json()["redirect_url"].endswith("/A000001")

    payment = Payment.objects.get(user=user)
    assert payment.status == Payment.Status.PENDING
    assert payment.authority == "A000001"
    # Amount is sent in Rial: PRO_PRICE_TOMAN (1_000_000 default) × 10.
    _, kwargs = m.call_args
    assert kwargs["json"]["amount"] == 10_000_000
    assert "callback_url" in kwargs["json"]


# ----- 2. request requires auth ---------------------------------------------


def test_request_requires_auth():
    assert APIClient().post("/api/billing/zarinpal/request/").status_code == 401


# ----- 3. gateway refusal -> 502, no Payment row ----------------------------


def test_request_gateway_refusal_returns_502(make_user):
    user = make_user(tier=User.Tier.FREE)
    # code != 100 and no authority -> request_payment raises ZarinpalError.
    bad = _fake_resp({"data": {"code": 12}})
    with mock.patch("billing.zarinpal.requests.post", return_value=bad):
        resp = _client(user).post("/api/billing/zarinpal/request/")
    assert resp.status_code == 502
    assert not Payment.objects.filter(user=user).exists()


# ----- 4. callback Status=OK + verify 100 -> PRO + expiry + verified ---------


def test_callback_ok_verify_100_activates_pro(make_user):
    user = make_user(tier=User.Tier.FREE)
    _seed_pending(user)
    client = APIClient()  # callback is unauthenticated (browser redirect)

    with mock.patch("billing.zarinpal.requests.post", return_value=VERIFY_100):
        resp = client.get("/api/billing/zarinpal/callback/?Authority=A000001&Status=OK")

    assert resp.status_code == 302
    assert "status=success" in resp["Location"]
    assert "ref_id=123456789" in resp["Location"]

    user.refresh_from_db()
    assert user.tier == User.Tier.PRO
    assert user.pro_expires_at is not None
    assert user.pro_expires_at > timezone.now()
    payment = Payment.objects.get(authority="A000001")
    assert payment.status == Payment.Status.VERIFIED
    assert payment.ref_id == "123456789"
    assert payment.verified_at is not None


# ----- 5. verify 101 (already verified) is idempotent -----------------------


def test_callback_verify_101_idempotent_no_double_activation(make_user):
    user = make_user(tier=User.Tier.FREE)
    _seed_pending(user)
    client = APIClient()

    with mock.patch("billing.zarinpal.requests.post", return_value=VERIFY_101):
        first = client.get("/api/billing/zarinpal/callback/?Authority=A000001&Status=OK")
    user.refresh_from_db()
    assert user.tier == User.Tier.PRO
    first_expiry = user.pro_expires_at

    # Replay the callback (page refresh / Zarinpal double-redirect). The Payment
    # is now VERIFIED, so activate_pro must no-op and the expiry must NOT shift.
    with mock.patch("billing.zarinpal.requests.post", return_value=VERIFY_101):
        second = client.get("/api/billing/zarinpal/callback/?Authority=A000001&Status=OK")
    assert first.status_code == second.status_code == 302
    user.refresh_from_db()
    assert user.pro_expires_at == first_expiry


def test_early_renewal_extends_existing_expiry(make_user):
    user = make_user(tier=User.Tier.PRO)
    old_expiry = timezone.now() + timedelta(days=100)
    user.pro_expires_at = old_expiry
    user.save(update_fields=["pro_expires_at"])
    _seed_pending(user)

    with mock.patch("billing.zarinpal.requests.post", return_value=VERIFY_100):
        APIClient().get("/api/billing/zarinpal/callback/?Authority=A000001&Status=OK")

    user.refresh_from_db()
    assert user.pro_expires_at > old_expiry + timedelta(days=364)


# ----- 6. Status=NOK (user cancelled) -> redirect cancel, no tier flip ------


def test_callback_status_nok_redirects_cancel(make_user):
    user = make_user(tier=User.Tier.FREE)
    _seed_pending(user)
    client = APIClient()

    resp = client.get("/api/billing/zarinpal/callback/?Authority=A000001&Status=NOK")
    assert resp.status_code == 302
    assert "status=cancel" in resp["Location"]
    user.refresh_from_db()
    assert user.tier == User.Tier.FREE
    # User backed out before paying — leave PENDING for reconciliation, not FAILED.
    assert Payment.objects.get(authority="A000001").status == Payment.Status.PENDING


# ----- 7. verify failure (code != 100/101) -> FAILED + cancel ---------------


def test_callback_verify_failure_marks_failed(make_user):
    user = make_user(tier=User.Tier.FREE)
    _seed_pending(user)
    client = APIClient()

    with mock.patch("billing.zarinpal.requests.post", return_value=VERIFY_FAILED):
        resp = client.get("/api/billing/zarinpal/callback/?Authority=A000001&Status=OK")
    assert resp.status_code == 302
    assert "status=cancel" in resp["Location"]
    user.refresh_from_db()
    assert user.tier == User.Tier.FREE
    # User went through the flow but the money did not land -> FAILED.
    assert Payment.objects.get(authority="A000001").status == Payment.Status.FAILED


# ----- 8. unknown authority -> error redirect (no crash, no state change) ----


def test_callback_unknown_authority_redirects_error(make_user):
    user = make_user(tier=User.Tier.FREE)
    client = APIClient()
    resp = client.get("/api/billing/zarinpal/callback/?Authority=BOGUS&Status=OK")
    assert resp.status_code == 302
    assert "status=error" in resp["Location"]
    assert not Payment.objects.filter(user=user).exists()

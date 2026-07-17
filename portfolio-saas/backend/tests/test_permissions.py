"""Tier-gated permissions + the C1 self-upgrade regression.

Free users must be blocked from Pro-only endpoints; the dangerous self-service
upgrade URL removed in the C2-fix pass must stay gone.
"""
import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def _auth(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_free_user_blocked_from_insights(make_user):
    resp = _auth(make_user(tier="FREE")).get("/api/insights/")
    assert resp.status_code == 403


def test_pro_user_can_access_insights(make_user):
    resp = _auth(make_user(tier="PRO")).get("/api/insights/")
    assert resp.status_code == 200
    body = resp.json()
    assert {"allocation", "concentration", "gold_band", "net_worth_trend"}.issubset(body)


def test_pro_check_gate(make_user):
    free = _auth(make_user(tier="FREE", email="free@test.test")).get("/api/auth/pro-check/")
    pro = _auth(make_user(tier="PRO", email="pro@test.test")).get("/api/auth/pro-check/")
    assert free.status_code == 403
    assert pro.status_code == 200


def test_insights_requires_authentication():
    """An anonymous request is rejected before the tier check even runs."""
    resp = APIClient().get("/api/insights/")
    assert resp.status_code in (401, 403)


def test_c1_regression_no_self_upgrade_endpoint():
    """C1 fix: the self-service upgrade URL must be gone (404), not callable."""
    client = APIClient()
    for method in ("post", "put", "patch"):
        resp = getattr(client, method)("/api/auth/upgrade/", {})
        assert resp.status_code == 404, f"{method.upper()} /api/auth/upgrade/ should not exist"

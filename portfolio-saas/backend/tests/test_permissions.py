"""Tier-gated permissions + the C1 self-upgrade regression.

Free users must be blocked from Pro-only endpoints; the dangerous self-service
upgrade URL removed in the C2-fix pass must stay gone.
"""
import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from accounts.features import RequiresFeature

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


# --- Per-tier portfolio ceiling (accounts.features registry) -----------------


def _create_portfolio(client, name):
    return client.post("/api/accounts/", {"name": name}, format="json")


def test_free_user_capped_at_three_portfolios(make_user):
    """Free tier stops at 3; the 4th is refused with a plan message."""
    client = _auth(make_user(tier="FREE"))
    for i in range(3):
        assert _create_portfolio(client, f"P{i}").status_code == 201

    resp = _create_portfolio(client, "P3")
    assert resp.status_code == 400
    assert "3 portfolios" in str(resp.json())


def test_pro_user_exceeds_the_free_ceiling(make_user):
    """Pro is unlimited by default, so the 4th portfolio succeeds."""
    client = _auth(make_user(tier="PRO", email="pro-limit@test.test"))
    for i in range(4):
        assert _create_portfolio(client, f"P{i}").status_code == 201


def test_ceiling_is_per_user_not_global(make_user):
    """One user filling their quota must not block another's first portfolio."""
    first = _auth(make_user(tier="FREE", email="a@test.test"))
    for i in range(3):
        assert _create_portfolio(first, f"P{i}").status_code == 201

    second = _auth(make_user(tier="FREE", email="b@test.test"))
    assert _create_portfolio(second, "P0").status_code == 201


def test_renaming_is_allowed_at_the_ceiling(make_user):
    """The cap gates creation only — an existing portfolio stays editable."""
    client = _auth(make_user(tier="FREE"))
    ids = [_create_portfolio(client, f"P{i}").json()["id"] for i in range(3)]

    resp = client.patch(f"/api/accounts/{ids[0]}/", {"name": "Renamed"}, format="json")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"


@override_settings(FREE_PORTFOLIO_LIMIT="1")
def test_ceiling_is_configurable_by_setting(make_user):
    """A deployment can tighten the ceiling without a code change."""
    client = _auth(make_user(tier="FREE"))
    assert _create_portfolio(client, "P0").status_code == 201
    assert _create_portfolio(client, "P1").status_code == 400


def test_registry_rejects_unknown_capability():
    """A typo in a gate name fails loudly at import, not silently at runtime."""
    with pytest.raises(ValueError):
        RequiresFeature("not_a_real_feature")

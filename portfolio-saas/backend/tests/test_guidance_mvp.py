from decimal import Decimal
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from portfolio.models import Account
from portfolio.optimization_models import OptimizationSnapshot, save_current_optimization


pytestmark = pytest.mark.django_db


def _payload(start, end):
    return {
        "target_weights": {"usd_cash": 1.0},
        "rebalance_trades": [],
        "data_window": {"start": start, "end": end},
    }


def test_guidance_discloses_short_history_and_keeps_account_private():
    user = User.objects.create_user(email="guidance@test.test", password="x")
    owned = Account.objects.create(user=user, name="Mine")
    other = User.objects.create_user(email="other-guidance@test.test", password="x")
    foreign = Account.objects.create(user=other, name="Other")
    OptimizationSnapshot.objects.create(
        account=None, scenario="risk_parity", window_days=365,
        payload=_payload("2025-09-01", "2026-09-01"),
    )
    client = APIClient()
    client.force_authenticate(user=user)

    with (
        patch("portfolio.views.analytics._current_weights_and_total", return_value=({"usd_cash": 1.0}, Decimal("100"), {})),
        patch("portfolio.services.returns.get_universe_by_mode", return_value=["usd_cash"]),
        patch("portfolio.views.analytics.optimize", side_effect=[
            _payload("2026-01-01", "2026-09-01"),
            _payload("2025-09-01", "2026-09-01"),
        ]) as solve,
    ):
        response = client.get(f"/api/guidance/?account={owned.pk}")

    assert response.status_code == 200
    assert response.data["risk_profile"] == "balanced"
    assert response.data["scenario"] == "risk_parity"
    assert response.data["personal_window_days"] == 365
    assert response.data["benchmark_window_days"] == 365
    assert response.data["fallback_disclosed"] is True
    assert solve.call_count == 2
    assert client.get(f"/api/guidance/?account={foreign.pk}").status_code == 404


def test_user_can_change_risk_profile_but_not_role():
    user = User.objects.create_user(email="profile@test.test", password="x")
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.patch(
        "/api/auth/me/", {"risk_profile": "growth", "role": "admin"}, format="json"
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.risk_profile == "growth"
    assert user.role == "user"
    assert user.is_staff is False and user.is_superuser is False


def test_optimization_current_row_is_replaced_per_exact_key():
    user = User.objects.create_user(email="upsert@test.test", password="x")
    account = Account.objects.create(user=user, name="Mine")
    first = save_current_optimization(
        account=account, scenario="my_optimal", window_days=0,
        payload={"version": 1},
    )
    second = save_current_optimization(
        account=account, scenario="my_optimal", window_days=0,
        payload={"version": 2},
    )

    assert second.pk == first.pk
    assert OptimizationSnapshot.objects.get(pk=first.pk).payload == {"version": 2}

"""Public backtest API integration tests.

Integration coverage fits because account ownership, quota reservation, run
creation, and task dispatch form one PostgreSQL-backed submission boundary.
"""
from unittest.mock import patch
import threading

import pytest
from django.db import close_old_connections, connections
from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import User
from portfolio.models import Account, Asset, BacktestRun, BacktestUserQuota, BacktestYear


pytestmark = pytest.mark.django_db


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_backtest_submission_uses_explicit_account_contract(
    make_user, django_capture_on_commit_callbacks
):
    user = make_user("pro-backtest@test.test", tier=User.Tier.PRO)
    account = Account.objects.create(user=user, name="Retirement")
    payload = {
        "account_id": account.id,
        "universe_mode": "portfolio",
        "symbols": [],
        "basis": "nominal_toman",
        "completed_years": 5,
    }

    with patch("portfolio.tasks.run_backtest_task.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = _client(user).post("/api/backtests/", payload, format="json")

    assert response.status_code == 201, response.data
    run = BacktestRun.objects.get(pk=response.data["id"])
    assert run.account == account
    assert run.universe_mode == "portfolio"
    assert run.basis == "nominal_toman"
    assert run.completed_years == 5
    assert BacktestUserQuota.objects.get(user=user).count == 1
    delay.assert_called_once_with(run.id)


def test_backtest_rejects_cross_account_submission(make_user):
    owner = make_user("owner-backtest@test.test", tier=User.Tier.PRO)
    intruder = make_user("intruder-backtest@test.test", tier=User.Tier.PRO)
    account = Account.objects.create(user=owner, name="Private")

    response = _client(intruder).post(
        "/api/backtests/",
        {
            "account_id": account.id,
            "universe_mode": "portfolio",
            "symbols": [],
            "basis": "nominal_toman",
            "completed_years": 5,
        },
        format="json",
    )

    assert response.status_code == 404
    assert not BacktestRun.objects.exists()


@pytest.mark.django_db(transaction=True)
@override_settings(DAILY_BACKTEST_LIMIT=1)
def test_concurrent_backtest_submissions_cannot_exceed_quota(make_user):
    user = make_user("quota-race@test.test", tier=User.Tier.PRO)
    account = Account.objects.create(user=user, name="Only")
    payload = {
        "account_id": account.id,
        "universe_mode": "portfolio",
        "symbols": [],
        "basis": "nominal_toman",
        "completed_years": 5,
    }
    barrier = threading.Barrier(2)
    statuses = []

    def submit():
        close_old_connections()
        thread_user = User.objects.get(pk=user.pk)
        barrier.wait()
        statuses.append(_client(thread_user).post(
            "/api/backtests/", payload, format="json"
        ).status_code)
        connections.close_all()

    with patch("portfolio.tasks.run_backtest_task.delay"):
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert sorted(statuses) == [201, 429]
    assert BacktestRun.objects.count() == 1
    assert BacktestUserQuota.objects.get(user=user).count == 1


def test_backtest_stability_summarizes_persistence(make_user):
    user = make_user("stability@test.test", tier=User.Tier.PRO)
    account = Account.objects.create(user=user, name="Stable")
    Asset.objects.create(key="gold", name="Gold", asset_class="Gold")
    Asset.objects.create(key="usd", name="USD", asset_class="Cash")
    run = BacktestRun.objects.create(
        user=user,
        account=account,
        params_hash="p",
        universe_hash="u",
        status=BacktestRun.Status.READY,
    )
    BacktestYear.objects.create(
        run=run,
        cutoff_date="1402-01-01",
        scenario="equal_weight",
        target_weights={"gold": 0.6, "usd": 0.4},
        realized_metrics={"net_return": 0.1},
        excluded_symbols=[{"symbol": "KAMA", "reason": "coverage_below_90pct"}],
    )
    BacktestYear.objects.create(
        run=run,
        cutoff_date="1403-01-01",
        scenario="equal_weight",
        target_weights={"gold": 1.0},
        realized_metrics={"net_return": 0.2},
        excluded_symbols=[{"symbol": "KAMA", "reason": "coverage_below_90pct"}],
    )

    response = _client(user).get(f"/api/backtests/{run.id}/stability/")

    assert response.status_code == 200, response.data
    assert response.data["asset_selection_frequency"]["gold"] == 1.0
    assert response.data["asset_selection_frequency"]["usd"] == 0.5
    assert response.data["average_weight"]["gold"] == 0.8
    assert response.data["asset_class_persistence"]["Gold"] == 1.0
    assert response.data["recurring_exclusion_reasons"]["coverage_below_90pct"] == 2

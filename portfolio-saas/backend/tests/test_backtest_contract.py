"""Public backtest API integration tests.

Integration coverage fits because account ownership, quota reservation, run
creation, and task dispatch form one PostgreSQL-backed submission boundary.
"""
from unittest.mock import patch
import threading

import pytest
from django.db import close_old_connections
from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import User
from portfolio.models import Account, BacktestRun, BacktestUserQuota


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
        close_old_connections()

    with patch("portfolio.tasks.run_backtest_task.delay"):
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert sorted(statuses) == [201, 429]
    assert BacktestRun.objects.count() == 1
    assert BacktestUserQuota.objects.get(user=user).count == 1

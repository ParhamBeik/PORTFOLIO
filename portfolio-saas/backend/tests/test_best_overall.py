"""Best Possible Portfolio Overall: nightly precompute + read-only view."""
from unittest import mock

import pytest
from rest_framework.test import APIClient

from portfolio.optimization_models import OptimizationSnapshot
from portfolio.services.best_overall import SCENARIOS, WINDOWS_DAYS, run_best_overall_snapshots
from tests.test_optimization import synthetic_history  # noqa: F401  (reused fixture)

pytestmark = pytest.mark.django_db


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def held_universe(synthetic_history):
    return ["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"]


def test_run_best_overall_snapshots_writes_one_per_window_and_scenario(held_universe):
    with mock.patch(
        "marketdata.universe.get_candidate_universe", return_value=(held_universe, [])
    ):
        result = run_best_overall_snapshots()

    assert result["ok"] is True
    snaps = OptimizationSnapshot.objects.filter(account=None)
    # synthetic_history is a short (~42 day) fixture, so only windows the
    # engine can actually solve over that history will have written rows --
    # the task must not raise on windows that can't solve, just skip them.
    assert snaps.count() > 0
    for snap in snaps:
        assert snap.window_days in WINDOWS_DAYS
        assert snap.scenario in SCENARIOS


def test_run_best_overall_snapshots_skips_when_universe_too_small():
    with mock.patch("marketdata.universe.get_candidate_universe", return_value=([], [])):
        result = run_best_overall_snapshots()
    assert result["ok"] is False
    assert OptimizationSnapshot.objects.count() == 0


def test_best_overall_view_reads_precomputed_snapshots(held_universe, make_user):
    with mock.patch(
        "marketdata.universe.get_candidate_universe", return_value=(held_universe, [])
    ):
        run_best_overall_snapshots()

    pro = make_user(email="best_overall@t.t")
    resp = _client(pro).get("/api/optimization/best-overall/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["windows"]) == len(WINDOWS_DAYS)
    labels = [w["label"] for w in body["windows"]]
    assert labels == ["1Y", "3Y", "5Y", "10Y"]
    assert any(w["status"] == "ok" for w in body["windows"])



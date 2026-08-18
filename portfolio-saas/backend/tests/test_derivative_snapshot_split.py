"""capture_derivative_snapshots: each of tse_option/ime_future/ime_option is
its own provider endpoint and must be its own failure domain. Before this
split, ime_futures/ime_options (evaluated after tse_option) repeatedly timed
out and re-raised, so tse_option's already-ingested rows were the only ones
ever reflected in the WorkflowRun ledger and the ime kinds never got an
independent success/failure signal (DerivativeContract held 1,111 tse_option
rows and zero ime_future/ime_option rows in production)."""
from unittest.mock import patch

import pytest

from marketdata.models import WorkflowRun
from marketdata.tasks import capture_derivative_snapshots

pytestmark = pytest.mark.django_db


def test_one_kinds_failure_does_not_block_the_others(settings):
    settings.TSETMC_API_KEY = "test-key"

    def fake_fetch(api_key, endpoint_key):
        if endpoint_key == "ime_futures":
            raise TimeoutError("ReadTimeout")
        return []

    with (
        patch("marketdata.fetchers.fetch_derivatives", side_effect=fake_fetch),
        patch("marketdata.ingest.ingest_derivative_snapshots", return_value=(3, 0)),
    ):
        results = capture_derivative_snapshots()

    assert results["tse_option"] == (3, 0)
    assert results["ime_option"] == (3, 0)
    assert isinstance(results["ime_future"], TimeoutError)

    runs = {
        run.workflow: run.outcome
        for run in WorkflowRun.objects.filter(workflow__startswith="capture_derivative_snapshots:")
    }
    assert runs["capture_derivative_snapshots:tse_option"] == WorkflowRun.Outcome.SUCCESS
    assert runs["capture_derivative_snapshots:ime_option"] == WorkflowRun.Outcome.SUCCESS
    assert runs["capture_derivative_snapshots:ime_future"] == WorkflowRun.Outcome.FAILED


def test_all_three_kinds_succeed_independently(settings):
    settings.TSETMC_API_KEY = "test-key"

    with (
        patch("marketdata.fetchers.fetch_derivatives", return_value=[]),
        patch("marketdata.ingest.ingest_derivative_snapshots", return_value=(1, 0)),
    ):
        results = capture_derivative_snapshots()

    assert results == {
        "tse_option": (1, 0),
        "ime_future": (1, 0),
        "ime_option": (1, 0),
    }
    assert WorkflowRun.objects.filter(
        workflow__startswith="capture_derivative_snapshots:",
        outcome=WorkflowRun.Outcome.SUCCESS,
    ).count() == 3

"""The ledger must say *why* a job retried, and must not grow forever."""
from datetime import timedelta

import pytest
from django.utils import timezone

from marketdata.models import WorkflowRun
from marketdata.tasks import _retry_code, prune_workflow_runs

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "last_error,expected",
    [
        # The three that account for 263 of 773 observed retries.
        ("Transient rate limit or network error: Provider request failed "
         "(ReadTimeout, status=None).", "ReadTimeout"),
        ("Transient rate limit or network error: Provider request failed "
         "(SSLError, status=None).", "SSLError"),
        ("Transient rate limit or network error: Provider request failed "
         "(ConnectionError, status=None).", "ConnectionError"),
        ("MarketDataFetchError: 1405-05-11: tick_volume_mismatch:38214512!=38014512",
         "tick_volume_mismatch"),
        ("MarketDataFetchError: Payload parsed to zero verifiable keys; parser and "
         "payload disagree.", "MarketDataFetchError"),
    ],
)
def test_retry_code_names_the_cause(last_error, expected):
    """A constant `archive_fetch_retry` made 44% of the ledger unqueryable."""
    assert _retry_code(last_error) == expected


def test_retry_code_never_returns_empty():
    """error_code is the grouping key; an empty one silently merges causes."""
    assert _retry_code("") == "archive_fetch_retry"


def test_prune_keeps_inside_window_and_drops_outside():
    now = timezone.now()
    for age_days, workflow in ((31, "old"), (29, "recent")):
        run = WorkflowRun.objects.create(workflow=workflow, outcome="success")
        # created_at is auto_now_add, so age it after the fact.
        WorkflowRun.objects.filter(pk=run.pk).update(
            created_at=now - timedelta(days=age_days)
        )

    assert prune_workflow_runs() == 1
    assert list(WorkflowRun.objects.values_list("workflow", flat=True)) == ["recent"]

from unittest import mock

import pytest

from marketdata.archive import claim_archive_batch
from marketdata.models import ArchiveFetchState
from marketdata.tasks import archive_tick


pytestmark = pytest.mark.django_db


def test_archive_tick_skips_when_lock_is_held(monkeypatch):
    import marketdata.tasks as tasks

    client = mock.Mock()
    client.set.return_value = False
    monkeypatch.setattr(tasks, "get_redis", lambda: client)
    ensure = mock.Mock()
    monkeypatch.setattr(tasks, "ensure_archive_states", ensure)

    archive_tick()

    ensure.assert_not_called()


def test_archive_claim_includes_each_due_endpoint_before_filling_priority(monkeypatch):
    import marketdata.archive as archive

    monkeypatch.setattr(archive, "remaining_requests", lambda bucket: 10)
    ArchiveFetchState.objects.create(endpoint="stock_history_unadjusted", symbol="price")
    ArchiveFetchState.objects.create(endpoint="stock_transaction_ticks", symbol="ticks")

    states = ArchiveFetchState.objects.filter(pk__in=claim_archive_batch(limit=2))
    assert set(states.values_list("endpoint", flat=True)) == {
        "stock_history_unadjusted", "stock_transaction_ticks",
    }

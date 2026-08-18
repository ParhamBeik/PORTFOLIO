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


def test_archive_tick_claims_only_free_queue_slots(monkeypatch):
    import marketdata.tasks as tasks

    client = mock.Mock()
    client.set.return_value = True
    monkeypatch.setattr(tasks, "get_redis", lambda: client)
    monkeypatch.setattr(tasks, "_queue_slots", lambda *_args: (3, 1))
    monkeypatch.setattr(tasks, "grow_tick_windows", mock.Mock())
    claim = mock.Mock(return_value=[])
    monkeypatch.setattr(tasks, "claim_archive_batch", claim)

    archive_tick()

    claim.assert_called_once_with(limit=3)


def test_archive_tick_does_not_claim_when_queue_is_full(monkeypatch):
    import marketdata.tasks as tasks

    client = mock.Mock()
    client.set.return_value = True
    monkeypatch.setattr(tasks, "get_redis", lambda: client)
    monkeypatch.setattr(tasks, "_queue_slots", lambda *_args: (0, 4))
    claim = mock.Mock()
    monkeypatch.setattr(tasks, "claim_archive_batch", claim)

    archive_tick()

    claim.assert_not_called()


def test_queue_slots_uses_broker_depth_and_fails_closed(monkeypatch):
    import marketdata.tasks as tasks

    broker = mock.Mock()
    broker.llen.return_value = 3
    monkeypatch.setattr("redis.Redis.from_url", lambda *_args, **_kwargs: broker)

    assert tasks._queue_slots("archive", 4) == (1, 3)

    broker.llen.side_effect = RuntimeError("broker unavailable")
    assert tasks._queue_slots("archive", 4) == (0, None)


def test_archive_claim_includes_each_due_endpoint_before_filling_priority(monkeypatch):
    import marketdata.archive as archive

    monkeypatch.setattr(archive, "remaining_requests", lambda bucket: 10)
    monkeypatch.setattr(archive, "_archive_prereqs_ready", lambda state: True)
    ArchiveFetchState.objects.create(endpoint="stock_history_unadjusted", symbol="price")
    ArchiveFetchState.objects.create(endpoint="stock_transaction_ticks", symbol="ticks")

    states = ArchiveFetchState.objects.filter(pk__in=claim_archive_batch(limit=2))
    assert set(states.values_list("endpoint", flat=True)) == {
        "stock_history_unadjusted", "stock_transaction_ticks",
    }

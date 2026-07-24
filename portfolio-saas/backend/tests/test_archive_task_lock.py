from unittest import mock

import pytest

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

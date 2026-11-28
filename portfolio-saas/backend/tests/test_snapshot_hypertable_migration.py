"""Unit tests for marketdata migration 0027_compress_snapshot_hypertables."""

import importlib
from unittest.mock import MagicMock


def test_migration_0027_noop_without_timescaledb():
    mod = importlib.import_module("marketdata.migrations.0027_compress_snapshot_hypertables")
    assert mod.Migration.atomic is False
    assert mod.Migration.dependencies == [
        ("marketdata", "0026_unsuspend_expanded_tgju_gold_daily_states"),
    ]

    cursor = MagicMock()
    cursor.fetchone.return_value = (False,)
    schema_editor = MagicMock()
    schema_editor.connection.cursor.return_value.__enter__.return_value = cursor

    mod.enable_snapshot_hypertables(None, schema_editor)
    mod.disable_compression_policies(None, schema_editor)

    assert cursor.execute.call_count == 2
    for call in cursor.execute.call_args_list:
        assert "pg_extension" in call.args[0]

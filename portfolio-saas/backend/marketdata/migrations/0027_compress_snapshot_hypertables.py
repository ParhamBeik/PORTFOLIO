"""Convert DerivativeSnapshot and MarketSnapshot to 7-day compressed hypertables.

Both tables append time-series snapshots queried by ``observed_at`` (unlike
``DailyStockHistory`` and ``MarketCandle``, whose readers filter on Jalali
``date`` varchar -- see ``0002_storage_tuning``). Converting them to
TimescaleDB hypertables with 7-day columnar compression is lossless and
keeps all queries and inserts working while shrinking ~5.2 GB to ~0.7 GB.

On plain PostgreSQL without the ``timescaledb`` extension (e.g. local dev or
unextended CI), both forward and reverse operations are clean no-ops.
"""

from django.db import migrations

_SNAPSHOT_TABLES = (
    ("marketdata_derivativesnapshot", "contract_id"),
    ("marketdata_marketsnapshot", "asset_class, symbol"),
)


def _has_timescaledb(cursor) -> bool:
    cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')")
    return bool(cursor.fetchone()[0])


def _is_hypertable(cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM timescaledb_information.hypertables "
        "WHERE hypertable_name = %s)",
        [table_name],
    )
    return bool(cursor.fetchone()[0])


def enable_snapshot_hypertables(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        if not _has_timescaledb(cursor):
            return
        for table_name, segmentby in _SNAPSHOT_TABLES:
            if not _is_hypertable(cursor, table_name):
                cursor.execute(f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {table_name}_pkey;")
                cursor.execute(f"CREATE INDEX IF NOT EXISTS {table_name}_id_idx ON {table_name} (id);")
                cursor.execute(
                    f"SELECT create_hypertable('{table_name}', 'observed_at', "
                    f"chunk_time_interval => INTERVAL '7 days', migrate_data => true, "
                    f"if_not_exists => true);"
                )
            cursor.execute(
                f"ALTER TABLE {table_name} SET (timescaledb.compress, "
                f"timescaledb.compress_segmentby = '{segmentby}', "
                f"timescaledb.compress_orderby = 'observed_at DESC');"
            )
            cursor.execute(
                f"SELECT add_compression_policy('{table_name}', INTERVAL '7 days', "
                f"if_not_exists => true);"
            )


def disable_compression_policies(apps, schema_editor):
    """Drop compression policies only; reverting a hypertable requires a restore."""
    with schema_editor.connection.cursor() as cursor:
        if not _has_timescaledb(cursor):
            return
        for table_name, _ in _SNAPSHOT_TABLES:
            cursor.execute(f"SELECT remove_compression_policy('{table_name}', if_exists => true)")


class Migration(migrations.Migration):
    atomic = False  # create_hypertable rewrites existing data outside one transaction

    dependencies = [("marketdata", "0026_unsuspend_expanded_tgju_gold_daily_states")]

    operations = [
        migrations.RunPython(enable_snapshot_hypertables, disable_compression_policies),
    ]

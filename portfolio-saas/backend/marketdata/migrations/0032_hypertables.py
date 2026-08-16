"""Convert the three big warehouse tables to compressed TimescaleDB hypertables.

The reason for this is COMPRESSION, not query speed. `stocktransactiontick`
grows ~743,836 rows and ~172 MB/day; at ~240 trading days that is ~41 GB/year
on a 250 GB VPS. Timescale's columnar compression on tick data typically wins
10-20x. If the compression policies below were ever removed, this migration
would be buying almost nothing and plain partitioning would be simpler.

Three constraints Timescale imposes, all handled here:

* A unique index must contain the partition column, so every natural key gains
  `ts`. This is not a semantic change: `ts` is a pure function of the Jalali
  date (and, for ticks, time) already in the key.
* A primary key must contain the partition column too. Nothing references these
  tables by foreign key, so the `id` PK is dropped and kept as a plain index --
  Django's ORM only needs `id` to exist, not to be DB-enforced.
* `ts` must be NOT NULL.

Compression is set to 30 days, NOT the usual 7: `marketdata/ingest.py` deletes a
(symbol, day) slice before re-inserting it, and the archive re-fetches within
MARKETDATA_TICK_WINDOW_DAYS (90). A shorter policy would mean routine ingest
constantly decompressing chunks it is about to rewrite.
"""
from django.db import migrations

# (table, unique constraint name, natural key columns, chunk interval)
_TABLES = (
    (
        "marketdata_stocktransactiontick",
        "uniq_stock_tick_symbol_date_row_time",
        ('symbol', 'date', '"row"', '"time"'),
        "1 day",
        "symbol",
        'ts, "row"',
    ),
    (
        "marketdata_dailystockhistory",
        "uniq_stock_history_symbol_date_adj",
        ("symbol", "date", "is_adjusted"),
        "30 days",
        "symbol",
        "ts",
    ),
    (
        "marketdata_marketcandle",
        "uniq_market_candle_symbol_tf_dt",
        ("symbol", "timeframe", "date_time"),
        "30 days",
        "symbol, timeframe",
        "ts",
    ),
)


def _base_sql():
    statements = []
    for table, uniq, key, interval, segment_by, order_by in _TABLES:
        cols = ", ".join(key)
        statements += [
            f"ALTER TABLE {table} ALTER COLUMN ts SET NOT NULL;",
            # Drop the id PK (Timescale requires the partition column in it) but
            # keep an index on id so ORM .get(pk=)/update/delete stay indexed.
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pkey;",
            f"CREATE INDEX IF NOT EXISTS {table}_id_idx ON {table} (id);",
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {uniq};",
            f"CREATE UNIQUE INDEX {uniq} ON {table} ({cols}, ts);",
        ]
    return statements


def _timescale_sql():
    statements = []
    for table, _uniq, _key, interval, segment_by, order_by in _TABLES:
        statements += [
            f"SELECT create_hypertable('{table}', 'ts', "
            f"chunk_time_interval => INTERVAL '{interval}', migrate_data => true);",
            f"ALTER TABLE {table} SET (timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{segment_by}', "
            f"timescaledb.compress_orderby = '{order_by}');",
            f"SELECT add_compression_policy('{table}', INTERVAL '30 days');",
        ]
    return statements


def forwards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb')"
        )
        has_timescaledb = cursor.fetchone()[0]
        for statement in _base_sql():
            cursor.execute(statement)
        if not has_timescaledb:
            return
        cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for statement in _timescale_sql():
            cursor.execute(statement)


def backwards(apps, schema_editor):
    """Drop policies only; turning hypertables back into plain tables is a restore."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')"
        )
        if not cursor.fetchone()[0]:
            return
        for table, *_rest in _TABLES:
            cursor.execute(f"SELECT remove_compression_policy('{table}', if_exists => true)")


class Migration(migrations.Migration):

    atomic = False  # create_hypertable rewrites ~10 GB; not one transaction

    dependencies = [("marketdata", "0031_backfill_ts")]

    operations = [migrations.RunPython(forwards, backwards)]

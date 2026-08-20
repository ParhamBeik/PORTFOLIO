"""Physical storage the warehouse needs but Django's schema layer cannot express.

Two things, both carried forward from the squashed history because a fresh
install genuinely needs them:

1. Autovacuum triggers. Postgres' default `autovacuum_analyze_scale_factor` of
   0.10 analyzes a table only once 10% of its rows have changed. On an
   append-only table with tens of millions of rows that threshold recedes as
   fast as the table grows, so it is never crossed. Measured, not theorised:
   `last_analyze` was NULL on every large table and the planner believed
   `stocktransactiontick` held 916,470 rows when it held 41,654,840 -- a 45x
   error, 291x on `dailystockhistory`. A flat row threshold with a zero scale
   factor makes the trigger absolute ("analyze after 50,000 new rows") so it
   keeps firing at any table size. 50,000 is roughly one trading day of ticks.

2. The tick hypertable. `stocktransactiontick` grows ~743,836 rows and
   ~172 MB/day; at ~240 trading days that is ~41 GB/year on a 250 GB VPS, and
   Timescale's columnar compression measured 28x on it. The reason is
   COMPRESSION, not query speed -- the same treatment was applied to the two
   daily tables and then reverted, because their readers filter on the Jalali
   `date` varchar rather than `ts`, so Timescale could not exclude chunks and
   spent 106 s planning against 2 s of execution.

   Timescale requires the partition column in every unique index and in the
   primary key, so the tick table's `id` PK becomes a plain index and its
   natural key gains `ts` (not a semantic change: `ts` is a pure function of the
   Jalali date and time already in the key). Compression is set to 30 days, NOT
   the usual 7, because `marketdata/ingest.py` deletes a (symbol, day) slice
   before re-inserting it and the archive re-fetches within
   MARKETDATA_TICK_WINDOW_DAYS (90) -- a shorter policy would mean routine
   ingest constantly decompressing chunks it is about to rewrite.

   Without the extension (CI, a plain-Postgres install) only step 1 of the tick
   work applies and the table stays an ordinary one.
"""
from django.db import migrations

_TICK = "marketdata_stocktransactiontick"
_TICK_UNIQUE = "uniq_stock_tick_symbol_date_row_time"

# (table, threshold, also tune vacuum). Tick re-ingest deletes a (symbol, day)
# slice before re-inserting it, so the three big tables accumulate dead tuples
# and need the absolute trigger for vacuum too, not just analyze.
_AUTOVACUUM = (
    ("marketdata_stocktransactiontick", 50000, True),
    ("marketdata_dailystockhistory", 50000, True),
    ("marketdata_marketcandle", 50000, True),
    ("marketdata_reallegalhistory", 50000, False),
    ("marketdata_goldcurrencyhistory", 10000, False),
    ("marketdata_workflowrun", 10000, False),
)


def _autovacuum_sql():
    statements = []
    for table, threshold, tune_vacuum in _AUTOVACUUM:
        options = [
            "autovacuum_analyze_scale_factor = 0.0",
            f"autovacuum_analyze_threshold = {threshold}",
        ]
        if tune_vacuum:
            options += [
                "autovacuum_vacuum_scale_factor = 0.0",
                f"autovacuum_vacuum_threshold = {threshold}",
            ]
        statements.append(f"ALTER TABLE {table} SET ({', '.join(options)});")
    return "\n".join(statements)


def _autovacuum_reset_sql():
    names = (
        "autovacuum_analyze_scale_factor, autovacuum_analyze_threshold, "
        "autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold"
    )
    return "\n".join(f"ALTER TABLE {t} RESET ({names});" for t, _, _ in _AUTOVACUUM)


def make_tick_hypertable(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {_TICK} ALTER COLUMN ts SET NOT NULL;")
        cursor.execute(f"ALTER TABLE {_TICK} DROP CONSTRAINT IF EXISTS {_TICK}_pkey;")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {_TICK}_id_idx ON {_TICK} (id);")
        cursor.execute(f"ALTER TABLE {_TICK} DROP CONSTRAINT IF EXISTS {_TICK_UNIQUE};")
        cursor.execute(f"DROP INDEX IF EXISTS {_TICK_UNIQUE};")
        cursor.execute(
            f'CREATE UNIQUE INDEX {_TICK_UNIQUE} ON {_TICK} (symbol, date, "row", "time", ts);'
        )
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb')"
        )
        if not cursor.fetchone()[0]:
            return
        cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        cursor.execute(
            f"SELECT create_hypertable('{_TICK}', 'ts', "
            f"chunk_time_interval => INTERVAL '1 day', migrate_data => true);"
        )
        cursor.execute(
            f"ALTER TABLE {_TICK} SET (timescaledb.compress, "
            f"timescaledb.compress_segmentby = 'symbol', "
            f"timescaledb.compress_orderby = 'ts, \"row\"');"
        )
        cursor.execute(f"SELECT add_compression_policy('{_TICK}', INTERVAL '30 days');")


def drop_compression_policy(apps, schema_editor):
    """Drop the policy only; turning a hypertable back into a plain table is a restore."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')")
        if cursor.fetchone()[0]:
            cursor.execute(f"SELECT remove_compression_policy('{_TICK}', if_exists => true)")


class Migration(migrations.Migration):
    atomic = False  # create_hypertable rewrites the table; not one transaction

    dependencies = [("marketdata", "0001_squashed")]

    operations = [
        migrations.RunSQL(sql=_autovacuum_sql(), reverse_sql=_autovacuum_reset_sql()),
        migrations.RunPython(make_tick_hypertable, drop_compression_policy),
    ]

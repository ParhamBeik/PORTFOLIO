"""Return the two daily tables to plain Postgres; keep ticks a hypertable.

0032 made all three big tables hypertables. Measured afterwards, that was right
for one of them and wrong for two:

    tick    8,483 MB -> 303 MB   28.0x    56 chunks    planning 1.7s
    candle  1,422 MB -> 399 MB    3.6x   432 chunks    planning 106s
    daily   1,269 MB -> 973 MB    1.3x   310 chunks    planning 106s

Two things went wrong on the daily tables. They hold 35 YEARS of daily rows, so
a 30-day chunk interval sized for tick volume produced hundreds of tiny chunks;
and every reader filters on the Jalali `date`/`date_time` varchar rather than
`ts`, so Timescale cannot exclude chunks and must plan a scan for each one.
`EXPLAIN` on one symbol's candles reported 106,512 ms of PLANNING against
2,103 ms of execution -- which is what made gunicorn workers time out.

The migration's own stated bar was "the value is compression, not query speed."
At 1.3x, `dailystockhistory` does not clear it. `marketcandle` at 3.6x is
closer, but it is the table feeding the returns matrix and the optimizer, and a
100-second planning cost there is not worth 1 GB.

The tick table keeps everything: 87% of the database, 28x compression, a
bounded 90-day window so chunk count stays near 90, and readers that always
narrow to one symbol and day.

Index definitions are captured before the swap and replayed after, so this does
not have to restate a schema that Django owns.
"""
from django.db import migrations

_TABLES = ("marketdata_dailystockhistory", "marketdata_marketcandle")


def forwards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cursor.fetchone() is None:
            # No Timescale here (test/CI Postgres), so 0032 never partitioned
            # these and there is nothing to undo.
            return
        for table in _TABLES:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = %s",
                [table],
            )
            index_defs = [row[0] for row in cursor.fetchall()]

            cursor.execute(
                "SELECT decompress_chunk(c, true) FROM show_chunks(%s) c", [table]
            )
            cursor.execute(
                "SELECT remove_compression_policy(%s, if_exists => true)", [table]
            )
            cursor.execute(f"ALTER TABLE {table} SET (timescaledb.compress = false)")

            # LIKE copies column types, defaults and NOT NULL but no indexes, so
            # the copy stays cheap and the captured definitions are replayed.
            cursor.execute(
                f"CREATE TABLE {table}__plain (LIKE {table} INCLUDING DEFAULTS "
                f"INCLUDING GENERATED INCLUDING IDENTITY)"
            )
            cursor.execute(f"INSERT INTO {table}__plain SELECT * FROM {table}")
            cursor.execute(f"DROP TABLE {table} CASCADE")
            cursor.execute(f"ALTER TABLE {table}__plain RENAME TO {table}")

            for definition in index_defs:
                # Timescale's internal per-chunk indexes live in another schema
                # and are gone with the hypertable.
                if "_timescaledb_internal" in definition:
                    continue
                cursor.execute(definition)

            cursor.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT max(id) FROM {table}), 1))"
            )


def backwards(apps, schema_editor):
    raise NotImplementedError(
        "Re-creating these as hypertables is migration 0032; reverting this one "
        "would reintroduce the 106s planning cost it exists to remove."
    )


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("marketdata", "0035_alter_dailystockhistory_ts_alter_marketcandle_ts_and_more"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]

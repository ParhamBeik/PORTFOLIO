"""Purge the `ts`-drift duplicates and take `ts` out of the identity keys.

`ts` is a Gregorian mirror of the Jalali domain key, derived at write time and
kept only as a range-partition dimension. It was also placed inside the unique
constraints on MarketCandle and DailyStockHistory. When the derivation formula
was later corrected (Tehran midnight is 20:30 UTC the *previous* day, not 03:30
UTC the same day), old rows kept the legacy value while new writes computed the
correct one -- so `bulk_create(ignore_conflicts=True)` stopped matching and every
re-ingest inserted a second copy of every row.

Measured on production 2026-08-24:

    MarketCandle          3,712,494 duplicate (symbol, timeframe, date) groups
                          of 7,531,859 rows -- 380,330 disagreeing on close
    DailyStockHistory       494,879 duplicate (symbol, date) groups

Neither table is a hypertable (only StockTransactionTick is), so nothing ever
required `ts` in these keys. Dropping it restores the Jalali string as the sole
identity, which is this warehouse's stated invariant.

A previous one-shot repair (0041_deduplicate_dailystockhistory_ts_drift, since
squashed away) cleaned the data but left the constraint alone, so the duplicates
came straight back. This migration fixes the generator first and only then the
data, and `JalaliDerivedDateTime.pre_save` no longer honours a caller-supplied
`ts`, so neither half can recur on its own.

Survivor rule: keep the row whose `ts` is Tehran-midnight-shaped (hour >= 19 UTC,
covering both the +03:30 and the pre-2022 +04:30 era). Verified against
production that no group holds two such rows, so exactly one survives per group.
Groups that hold ONLY legacy rows (77,006 candles, 136 history) keep their data
and have `ts` recomputed instead -- those are real rows, not duplicates.
"""
from django.db import migrations, models

# Tehran is +03:30 (and was +04:30 before 2022), so local midnight is always
# 19:30 or 20:30 UTC on the PREVIOUS day. The legacy values sit at 03:30/04:30.
# Verified exhaustively against production: those four times are the only ones
# present in either table, so the hour alone separates the two populations.
def _is_correct_ts(alias=""):
    prefix = f"{alias}." if alias else ""
    # `AT TIME ZONE 'UTC'` is explicit on purpose. Bare EXTRACT on a timestamptz
    # reads it in the SESSION timezone, so this predicate -- which decides which
    # of 4.2M rows gets DELETED -- would silently invert under a connection that
    # is not UTC. Django sets UTC today; a destructive migration should not
    # depend on that staying true.
    return f"EXTRACT(HOUR FROM {prefix}ts AT TIME ZONE 'UTC') >= 19"


_BATCH = 200_000

_TABLES = (
    ("marketdata_marketcandle", ("symbol", "timeframe", "date_time"), "date_time"),
    ("marketdata_dailystockhistory", ("symbol", "date"), "date"),
)


def _delete_legacy_duplicates(cursor, table, key_fields):
    """Drop legacy-`ts` rows that have a correct-`ts` twin, in bounded batches.

    Batched because production runs a 15-minute `statement_timeout` and a single
    3.7M-row DELETE would be killed by it -- the tick backfill already loses jobs
    that way.
    """
    keys = " AND ".join(f"d.{name} = t.{name}" for name in key_fields)
    sql = f"""
        DELETE FROM {table}
        WHERE id IN (
            SELECT t.id FROM {table} t
            WHERE NOT ({_is_correct_ts('t')})
              AND EXISTS (
                  SELECT 1 FROM {table} d
                  WHERE {keys} AND {_is_correct_ts('d')}
              )
            LIMIT {_BATCH}
        )
    """
    while True:
        cursor.execute(sql)
        if not cursor.rowcount:
            return


def _repair_orphan_ts(cursor, table, date_field):
    """Recompute `ts` for rows that had no correct-`ts` twin to defer to.

    Done per distinct Jalali date rather than per row: SQL cannot convert a
    Jalali string at all (`1402-02-30` is not a Gregorian date), and there are a
    few thousand distinct dates behind ~77,000 rows.
    """
    from marketdata import jalali

    cursor.execute(
        f"SELECT DISTINCT {date_field} FROM {table} WHERE NOT ({_is_correct_ts()})"
    )
    for (raw,) in cursor.fetchall():
        corrected = jalali.to_datetime(str(raw).split()[0])
        if corrected is None:
            # Unparseable date: leave the row alone. `ts` is only a partition
            # mirror, and inventing a timestamp for a row whose domain key is
            # already broken helps nobody.
            continue
        cursor.execute(
            f"UPDATE {table} SET ts = %s "
            f"WHERE {date_field} = %s AND NOT ({_is_correct_ts()})",
            [corrected, raw],
        )


def purge_ts_drift(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for table, key_fields, date_field in _TABLES:
            _delete_legacy_duplicates(cursor, table, key_fields)
            _repair_orphan_ts(cursor, table, date_field)

        # Left behind by the earlier repair; 1.4 GB of a table nothing reads.
        cursor.execute(
            "DROP TABLE IF EXISTS marketdata_dailystockhistory_ts_dedup_backup"
        )


def drop_ts_identity_uniques(apps, schema_editor):
    """Drop whichever unique still contains `ts`, whatever it is named.

    Production never had the squashed name `uniq_market_candle_symbol_tf_dt`
    (the live unique was created under a different name before the squash),
    so a hardcoded RemoveConstraint aborts after the data purge. Look the
    constraint up from the catalog and drop it; unique indexes that are not
    constraints get the same treatment.
    """
    with schema_editor.connection.cursor() as cursor:
        for table, _keys, _date in _TABLES:
            cursor.execute(
                """
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = %s AND c.contype = 'u'
                  AND EXISTS (
                    SELECT 1
                    FROM unnest(c.conkey) AS ck(attnum)
                    JOIN pg_attribute a
                      ON a.attrelid = t.oid AND a.attnum = ck.attnum
                    WHERE a.attname = 'ts'
                  )
                """,
                [table],
            )
            for (name,) in cursor.fetchall():
                cursor.execute(
                    f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{name}"'
                )
            cursor.execute(
                """
                SELECT i.relname
                FROM pg_index x
                JOIN pg_class t ON t.oid = x.indrelid
                JOIN pg_class i ON i.oid = x.indexrelid
                JOIN pg_attribute a
                  ON a.attrelid = t.oid AND a.attnum = ANY (x.indkey)
                WHERE t.relname = %s
                  AND x.indisunique AND NOT x.indisprimary
                  AND a.attname = 'ts'
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid
                  )
                """,
                [table],
            )
            for (idx,) in cursor.fetchall():
                cursor.execute(f'DROP INDEX IF EXISTS "{idx}"')


class Migration(migrations.Migration):

    dependencies = [
        ('marketdata', '0004_api_quota_per_plan'),
    ]

    operations = [
        # Data first: AddConstraint below would fail outright while duplicates
        # are still present.
        migrations.RunPython(purge_ts_drift, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(drop_ts_identity_uniques, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name='marketcandle',
                    name='uniq_market_candle_symbol_tf_dt',
                ),
                migrations.RemoveConstraint(
                    model_name='dailystockhistory',
                    name='uniq_stock_history_symbol_date_adj',
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name='marketcandle',
            constraint=models.UniqueConstraint(
                fields=('symbol', 'timeframe', 'date_time'),
                name='uniq_market_candle_symbol_tf_dt',
            ),
        ),
        migrations.AddConstraint(
            model_name='dailystockhistory',
            constraint=models.UniqueConstraint(
                fields=('symbol', 'date'),
                name='uniq_stock_history_symbol_date_adj',
            ),
        ),
    ]

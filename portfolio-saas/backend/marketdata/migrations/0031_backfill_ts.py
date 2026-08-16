"""Backfill the `ts` partition column from each table's Jalali key.

Done per DISTINCT Jalali date rather than per row: the calendar conversion is a
pure function of the date string, and the three tables hold 56 / 6,131 / 8,549
distinct dates against 41.6M / 4.0M / 3.8M rows. Converting in Python and
mapping back with a VALUES join turns tens of millions of Python calls into a
few thousand, with Postgres doing the row-level work.

Ticks additionally carry a time-of-day, added SQL-side by casting the existing
`time` varchar to an interval, so `ts` is the real instant of the trade rather
than midnight. Times are Tehran local (what the provider quotes) and stored as
an absolute instant.
"""
from django.db import migrations

# Tables that carry only a date, so `ts` is midnight Tehran on that day.
_DATE_ONLY = (
    ("marketdata_dailystockhistory", "date"),
    ("marketdata_marketcandle", "date_time"),
)
_TICKS = "marketdata_stocktransactiontick"
_CHUNK = 500


def _mapping(cursor, table, column):
    """[(jalali, gregorian_date)] for every distinct value that parses."""
    from marketdata import jalali

    cursor.execute(
        f'SELECT DISTINCT "{column}" FROM {table} WHERE ts IS NULL'  # noqa: S608
    )
    pairs = []
    for (value,) in cursor.fetchall():
        gregorian = jalali.to_gregorian(value)
        if gregorian is not None:
            pairs.append((value, gregorian.isoformat()))
    return pairs


def forwards(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        for table, column in _DATE_ONLY:
            pairs = _mapping(cursor, table, column)
            for start in range(0, len(pairs), _CHUNK):
                batch = pairs[start:start + _CHUNK]
                placeholders = ",".join(["(%s, %s::date)"] * len(batch))
                cursor.execute(
                    f"UPDATE {table} AS t SET ts = m.g AT TIME ZONE 'Asia/Tehran' "  # noqa: S608
                    f"FROM (VALUES {placeholders}) AS m(j, g) "
                    f'WHERE t."{column}" = m.j AND t.ts IS NULL',
                    [item for pair in batch for item in pair],
                )

        pairs = _mapping(cursor, _TICKS, "date")
        for start in range(0, len(pairs), _CHUNK):
            batch = pairs[start:start + _CHUNK]
            placeholders = ",".join(["(%s, %s::date)"] * len(batch))
            # A malformed time must not abort the batch or silently become
            # midnight on a different day: fall back to the date's own midnight.
            cursor.execute(
                f"UPDATE {_TICKS} AS t SET ts = "  # noqa: S608
                f"  (m.g + COALESCE(NULLIF(t.time, '')::interval, '0'::interval)) "
                f"  AT TIME ZONE 'Asia/Tehran' "
                f"FROM (VALUES {placeholders}) AS m(j, g) "
                f"WHERE t.date = m.j AND t.ts IS NULL",
                [item for pair in batch for item in pair],
            )


def backwards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for table, _column in (*_DATE_ONLY, (_TICKS, "date")):
            cursor.execute(f"UPDATE {table} SET ts = NULL")  # noqa: S608


class Migration(migrations.Migration):

    atomic = False  # 41.6M rows; do not hold one transaction open for all of it

    dependencies = [
        ("marketdata", "0030_dailystockhistory_ts_marketcandle_ts_and_more"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]

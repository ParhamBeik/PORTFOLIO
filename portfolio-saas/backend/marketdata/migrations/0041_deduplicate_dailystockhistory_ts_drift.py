"""Deduplicate DailyStockHistory rows created by a ts-derivation drift.

`ts` (marketdata/models.py JalaliDerivedDateTime) is meant to be a pure
function of `date`: Tehran midnight for that Jalali day, converted to UTC.
It has not always been computed that way -- migration 0032's original backfill
used different math, and a more recent regression (traced to correlation IDs
ingested 2026-08-15/16, fixed by 2026-08-17) computed it as UTC-midnight-plus-
offset instead of Tehran-midnight-in-UTC, a full 7 hours off. Since `ts` is
part of this table's uniqueness key, every time a full-history re-fetch landed
on a date whose stored `ts` no longer matched the current formula, the upsert
could not find its own row to update and inserted a second one instead.

Verified before writing this (see the session's investigation): of the
3,490,447 affected (symbol, date) pairs, every single one has *exactly one*
row whose `ts` matches `jalali.to_datetime(date)` as computed today. The
non-partition fields disagree in two ways:

* Nullable Real/Legal breakdown columns (buy_count_i and 11 siblings): differ
  in 1,402,761 pairs, and in every one of those it is a clean NULL-vs-value
  case (ingest_real_legal's `.update()` only ever reached one of the two rows
  when they diverged) -- a pure COALESCE onto the surviving row loses nothing.
* Core price fields (pl, pc, tvol, ...): differ in fewer than 700 pairs total.
  These are NOT NULL columns with real values on both sides, always small
  numeric differences on old, already-settled dates. The surviving row (the
  one with the currently-correct `ts`) is the one written by the current,
  validated ingest path, so its own value is kept rather than guessed at.

Every row this migration touches -- both the row kept and the row deleted --
is copied to `marketdata_dailystockhistory_ts_dedup_backup` first, so
`backwards` can restore the pre-merge state exactly. This does not fix `ts`
derivation itself (already correct in marketdata/jalali.py); it only cleans
up the duplicate rows that drift already produced.
"""
from django.db import migrations

BACKUP_TABLE = "marketdata_dailystockhistory_ts_dedup_backup"

# Nullable columns only -- see docstring. NOT NULL columns (pl, pc, py, plc,
# pcc, plp, pcp, tno, tvol, tval) are deliberately left as the surviving row's
# own value.
NULLABLE_MERGE_FIELDS = (
    "pmin", "pmax", "pf",
    "buy_count_i", "buy_count_n", "sell_count_i", "sell_count_n",
    "buy_i_volume", "buy_n_volume", "sell_i_volume", "sell_n_volume",
    "buy_i_value", "buy_n_value", "sell_i_value", "sell_n_value",
)


def forwards(apps, schema_editor):
    from marketdata import jalali

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} AS "
            "SELECT * FROM marketdata_dailystockhistory WHERE 1=0"
        )

        cursor.execute("SELECT DISTINCT date FROM marketdata_dailystockhistory")
        dates = [row[0] for row in cursor.fetchall()]
        date_map = [(d, jalali.to_datetime(d)) for d in dates]
        date_map = [(d, ts) for d, ts in date_map if ts is not None]
        if not date_map:
            return

        cursor.execute(
            "CREATE TEMP TABLE _ts_correct (jdate text PRIMARY KEY, correct_ts timestamptz)"
        )
        with cursor.copy("COPY _ts_correct (jdate, correct_ts) FROM STDIN") as copy:
            for jdate, ts in date_map:
                copy.write_row((jdate, ts))
        cursor.execute("ANALYZE _ts_correct")

        cursor.execute(
            "CREATE TEMP TABLE _dedup_pairs AS "
            "SELECT symbol, date FROM marketdata_dailystockhistory "
            "GROUP BY symbol, date HAVING count(*) = 2"
        )
        cursor.execute("CREATE INDEX ON _dedup_pairs (symbol, date)")
        # Freshly created temp tables carry no planner statistics, so without
        # this every join below risks a nested loop over millions of rows
        # instead of a hash join -- verified the hard way (a first attempt at
        # this migration ran the merge UPDATE for 30+ minutes with no plan
        # information before it was cancelled).
        cursor.execute("ANALYZE _dedup_pairs")

        cursor.execute(
            "SELECT count(*) FROM _dedup_pairs"
        )
        if cursor.fetchone()[0] == 0:
            return

        cursor.execute(
            "CREATE TEMP TABLE _dedup_roles AS "
            "SELECT h.id, (h.ts = tc.correct_ts) AS is_correct, p.symbol, p.date "
            "FROM _dedup_pairs p "
            "JOIN marketdata_dailystockhistory h ON h.symbol = p.symbol AND h.date = p.date "
            "JOIN _ts_correct tc ON tc.jdate = p.date"
        )
        cursor.execute("CREATE UNIQUE INDEX ON _dedup_roles (id)")
        cursor.execute("CREATE INDEX ON _dedup_roles (symbol, date)")
        cursor.execute("ANALYZE _dedup_roles")

        # Sanity gate: only proceed where the pattern is exactly what was
        # verified (one correct, one wrong row per pair). Abort loudly on any
        # pair this migration was not designed for, rather than guessing.
        cursor.execute(
            "SELECT count(*) FROM ("
            "  SELECT symbol, date, count(*) FILTER (WHERE is_correct) AS n"
            "  FROM _dedup_roles GROUP BY symbol, date"
            ") t WHERE n != 1"
        )
        unexpected = cursor.fetchone()[0]
        if unexpected:
            raise RuntimeError(
                f"{unexpected} duplicate DailyStockHistory pair(s) do not match "
                "the expected exactly-one-correct-row pattern; aborting rather "
                "than guessing which row to keep. Re-investigate before retrying."
            )

        cursor.execute(
            "CREATE TEMP TABLE _dedup_map AS "
            "SELECT row_number() OVER () AS rn, rk.id AS keep_id, rd.id AS drop_id "
            "FROM _dedup_roles rk "
            "JOIN _dedup_roles rd ON rd.symbol = rk.symbol AND rd.date = rk.date "
            "  AND rd.id != rk.id "
            "WHERE rk.is_correct = true"
        )
        cursor.execute("CREATE UNIQUE INDEX ON _dedup_map (rn)")
        cursor.execute("CREATE UNIQUE INDEX ON _dedup_map (keep_id)")
        cursor.execute("CREATE UNIQUE INDEX ON _dedup_map (drop_id)")
        cursor.execute("ANALYZE _dedup_map")

        # Back up every row about to be touched (both kept and dropped) before
        # any mutation, so backwards() can restore the exact pre-merge state.
        cursor.execute(
            f"INSERT INTO {BACKUP_TABLE} "
            "SELECT h.* FROM marketdata_dailystockhistory h "
            "JOIN _dedup_roles r ON r.id = h.id"
        )

        cursor.execute("SELECT count(*) FROM _dedup_map")
        total_pairs = cursor.fetchone()[0]

        merge_sets = ", ".join(
            f"{field} = COALESCE(keep.{field}, d.{field})"
            for field in NULLABLE_MERGE_FIELDS
        )
        # Batched, not one 3.49M-row hash join: this container's Postgres
        # runs with a 1GB memory ceiling, and a single all-at-once join over
        # ~11M + 3.49M rows spilled its hash table to disk -- 196 GB of block
        # I/O and still not done after 15+ minutes on the first real attempt.
        # A 200k-row batch's working set stays comfortably in memory.
        batch_size = 200_000
        for start in range(1, total_pairs + 1, batch_size):
            end = min(start + batch_size - 1, total_pairs)
            cursor.execute(
                f"UPDATE marketdata_dailystockhistory AS keep "
                f"SET {merge_sets} "
                "FROM _dedup_map m "
                "JOIN marketdata_dailystockhistory d ON d.id = m.drop_id "
                "WHERE keep.id = m.keep_id AND m.rn BETWEEN %s AND %s",
                [start, end],
            )
            cursor.execute(
                "DELETE FROM marketdata_dailystockhistory "
                "USING _dedup_map m "
                "WHERE marketdata_dailystockhistory.id = m.drop_id "
                "AND m.rn BETWEEN %s AND %s",
                [start, end],
            )

        # Final assertion: no (symbol, date) pair should have more than one
        # row left. A failure here means the merge/delete did not do what the
        # sanity gate above promised, which must never pass silently.
        cursor.execute(
            "SELECT count(*) FROM ("
            "  SELECT symbol, date FROM marketdata_dailystockhistory "
            "  GROUP BY symbol, date HAVING count(*) > 1"
            ") t"
        )
        remaining = cursor.fetchone()[0]
        if remaining:
            raise RuntimeError(
                f"{remaining} (symbol, date) pair(s) still have duplicate rows "
                "after dedup; aborting the transaction."
            )


def backwards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT to_regclass('{BACKUP_TABLE}')")
        if cursor.fetchone()[0] is None:
            return
        # Restore the deleted rows first (their ids no longer exist).
        cursor.execute(
            "INSERT INTO marketdata_dailystockhistory "
            f"SELECT b.* FROM {BACKUP_TABLE} b "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM marketdata_dailystockhistory h WHERE h.id = b.id"
            ")"
        )
        # Revert the surviving rows' merged fields back to their pre-merge state.
        restore_sets = ", ".join(f"{field} = b.{field}" for field in NULLABLE_MERGE_FIELDS)
        cursor.execute(
            f"UPDATE marketdata_dailystockhistory AS keep "
            f"SET {restore_sets} "
            f"FROM {BACKUP_TABLE} b "
            "WHERE keep.id = b.id"
        )


class Migration(migrations.Migration):

    dependencies = [
        ('marketdata', '0040_drop_dailystockhistory_is_adjusted'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

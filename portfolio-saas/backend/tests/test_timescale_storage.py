"""The tick hypertable as production runs it: TimescaleDB, not plain Postgres.

CI used to run plain `postgres:16`, where migration 0002 skips the hypertable
and 0020 skips the compression policy, and every Timescale-aware reader takes
its fallback branch. So the shape production depends on -- the hypertable on
`ts`, the unique index that must carry `ts`, 7-day compression, and replace
ingest landing on a compressed chunk -- was never exercised before a deploy.
CI now runs the same `timescale/timescaledb` image as docker-compose.prod.yml.

Locally these skip when the extension is absent. CI sets REQUIRE_TIMESCALEDB=1
so a CI database without it fails here instead of quietly skipping the lot.
"""
import os

import pytest
from django.db import connection

_TICK = "marketdata_stocktransactiontick"
_TICK_UNIQUE = "uniq_stock_tick_symbol_date_row_time"


def _has_timescaledb():
    with connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')")
        return cursor.fetchone()[0]


@pytest.fixture
def timescale(db):
    if not _has_timescaledb():
        if os.getenv("REQUIRE_TIMESCALEDB") == "1":
            pytest.fail("REQUIRE_TIMESCALEDB=1 but the test database has no timescaledb extension")
        pytest.skip("timescaledb extension not installed in the test database")


def _one(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


def test_tick_table_is_a_hypertable_partitioned_on_ts(timescale):
    row = _one(
        "SELECT column_name, time_interval FROM timescaledb_information.dimensions "
        "WHERE hypertable_name = %s",
        [_TICK],
    )
    assert row is not None, "migration 0002 did not convert the tick table"
    column, interval = row
    assert column == "ts"
    assert interval.days == 1


def test_tick_unique_index_carries_the_partition_column(timescale):
    """TimescaleDB rejects a unique index on a hypertable without `ts` (CLAUDE.md)."""
    (indexdef,) = _one(
        "SELECT indexdef FROM pg_indexes WHERE tablename = %s AND indexname = %s",
        [_TICK, _TICK_UNIQUE],
    )
    assert "UNIQUE" in indexdef
    assert "(symbol, date, \"row\", \"time\", ts)" in indexdef


def test_tick_compression_segments_by_symbol_after_seven_days(timescale):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT attname, segmentby_column_index, orderby_column_index "
            "FROM timescaledb_information.compression_settings WHERE hypertable_name = %s",
            [_TICK],
        )
        settings = {name: (seg, order) for name, seg, order in cursor.fetchall()}
    assert settings["symbol"][0] == 1
    assert settings["ts"][1] == 1 and settings["row"][1] == 2

    (compress_after,) = _one(
        "SELECT config->>'compress_after' FROM timescaledb_information.jobs "
        "WHERE hypertable_name = %s AND proc_name = 'policy_compression'",
        [_TICK],
    )
    assert compress_after == "7 days", "migration 0020 sets the policy to 7 days"


def test_replace_ingest_rewrites_a_compressed_day(timescale):
    """The archive re-fetches days whose chunks are already compressed.

    `ingest_transactions(replace=True)` deletes the (symbol, day) slice and
    inserts it again. On TimescaleDB that DML hits a compressed chunk, which
    plain Postgres cannot reproduce at all.
    """
    from marketdata import ingest
    from marketdata.models import StockTransactionTick

    payload = [
        {"row": 1, "time": "09:15:22", "price": 4200, "volume": 5},
        {"row": 2, "time": "09:15:23", "price": 4300, "volume": 6},
    ]
    ingest.ingest_transactions("SYM", "1405-05-24", payload, replace=True)
    ingest.ingest_transactions("SYM", "1405-05-25", payload, replace=True)

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT compress_chunk(c) FROM show_chunks('{_TICK}') c")
        compressed = cursor.fetchall()
    assert compressed, "the ingested days produced no chunks to compress"

    ingest.ingest_transactions(
        "SYM", "1405-05-24",
        [{"row": 1, "time": "10:00:00", "price": 9900, "volume": 7}],
        replace=True,
    )

    day = StockTransactionTick.objects.filter(symbol="SYM", date="1405-05-24")
    assert day.count() == 1, "the compressed rows for the replaced day must be gone"
    assert int(day.first().price) == 9900
    assert StockTransactionTick.objects.filter(symbol="SYM", date="1405-05-25").count() == 2


def test_ops_reads_the_newest_tick_day_from_chunk_metadata(timescale):
    """`_latest_tick_day` answers from chunk ranges; only Timescale has them."""
    from marketdata import admin_telemetry, ingest

    payload = [{"row": 1, "time": "09:15:22", "price": 4200, "volume": 5}]
    ingest.ingest_transactions("SYM", "1405-05-24", payload, replace=True)
    ingest.ingest_transactions("SYM", "1405-05-26", payload, replace=True)

    assert admin_telemetry._hypertables() == {_TICK}
    assert admin_telemetry._latest_tick_day() == "1405-05-26"

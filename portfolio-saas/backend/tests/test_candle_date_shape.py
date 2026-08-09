"""`MarketCandle.date_time` must hold the bare Jalali day and nothing else.

Every calendar lookup in the archive is a `date_time__in=[bare dates]`, so a row
stored as "1405-05-11 00:00:00" is invisible to the trading calendar, to
`_symbol_candle_volumes`, and therefore to the whole tick pipeline. 5,654 legacy
rows were in that shape, all belonging to کاما, 32 of them inside the active
90-day window.
"""
import pytest
from django.core.management import call_command

from marketdata import ingest
from marketdata.models import MarketCandle

pytestmark = pytest.mark.django_db


def test_ingest_never_writes_a_timestamp():
    """The write path is closed: validation rejects a timestamped date outright.

    So the 5,654 legacy rows cannot be re-created, and the clean day beside them
    still lands. This pins the behaviour rather than assuming it.
    """
    accepted, rejected = ingest.ingest_candles(
        "TEST",
        2,
        {"candle_daily": [
            {"date": "1405-05-11 00:00:00", "close": 100, "volume": 5},
            {"date": "1405-05-12", "close": 101, "volume": 6},
        ]},
    )

    assert rejected == 1
    assert set(MarketCandle.objects.values_list("date_time", flat=True)) == {"1405-05-12"}


def test_normalize_renames_legacy_rows_and_drops_exact_duplicates():
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED,
        date_time="1395-12-17 00:00:00", close_price=62, volume=1458107,
    )
    # Same day already present in the bare shape: the long row adds nothing.
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED,
        date_time="1395-12-17", close_price=62, volume=1458107,
    )
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED,
        date_time="1395-12-18 00:00:00", close_price=63, volume=10,
    )

    call_command("normalize_candle_dates", apply=True)

    rows = set(MarketCandle.objects.values_list("date_time", "close_price"))
    assert {day for day, _ in rows} == {"1395-12-17", "1395-12-18"}
    assert len(rows) == 2


def test_dry_run_changes_nothing():
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED,
        date_time="1395-12-18 00:00:00", close_price=63, volume=10,
    )

    call_command("normalize_candle_dates")

    assert MarketCandle.objects.get().date_time == "1395-12-18 00:00:00"

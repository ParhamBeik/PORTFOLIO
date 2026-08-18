"""Make a symbol match what the provider serves today.

کاما's candle table held 8,177 unadjusted rows against 4,936 the provider lists.
The 3,240 extras all carried a volume of exactly 10,000,000 -- synthetic fill
that had grown to outnumber the real rows. Arguing row by row is slower and less
certain than rebuilding the series from the only authority on it.
"""
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from marketdata.models import DailyStockHistory, MarketCandle

pytestmark = pytest.mark.django_db

HISTORY = [
    # `pc` (closing price) is required by validation and is present on every real
    # History.php record; omitting it here rejected the rows as price_not_numeric.
    {"date": "1405-05-11", "pf": 100, "pmax": 110, "pmin": 95, "pl": 105,
     "pc": 105, "tvol": 5000},
    {"date": "1405-05-12", "pf": 105, "pmax": 115, "pmin": 100, "pl": 112,
     "pc": 112, "tvol": 6000},
]
ADJUSTED = {"candle_daily_adjusted": [
    {"date": "1405-05-11", "open": 50, "high": 55, "low": 47, "close": 52, "volume": 5000},
    {"date": "1405-05-12", "open": 52, "high": 57, "low": 50, "close": 56, "volume": 6000},
]}


def _run(**kwargs):
    # apply mode fetches twice and requires agreement, so serve stable data.
    with patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_daily_history",
        return_value=HISTORY,
    ), patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_candlesticks",
        return_value=ADJUSTED,
    ):
        # The closed-market guard is tested separately; keep these deterministic.
        kwargs.setdefault("force_during_session", True)
        call_command("resync_symbol_from_provider", "کاما", **kwargs)


def test_rows_on_dates_the_provider_never_lists_are_deleted():
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-02", close_price=2974, volume=10_000_000,
    )
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-11", close_price=105, volume=5000,
    )

    _run(apply=True)

    days = set(
        MarketCandle.objects.filter(
            symbol="کاما", timeframe=MarketCandle.UNADJUSTED
        ).values_list("date_time", flat=True)
    )
    assert days == {"1405-05-11", "1405-05-12"}


def test_a_wrong_value_on_a_real_date_is_rewritten_not_deleted():
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-11", close_price=10.5, volume=5000,  # 10x low
    )

    _run(apply=True)

    row = MarketCandle.objects.get(symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
                                   date_time="1405-05-11")
    assert float(row.close_price) == 105


def test_dry_run_writes_nothing():
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-02", close_price=2974, volume=10_000_000,
    )

    _run()

    assert MarketCandle.objects.filter(date_time="1405-05-02").exists()


def test_a_timeframe_the_provider_does_not_publish_is_left_alone():
    """No adjusted feed must not mean "delete the adjusted series"."""
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED,
        date_time="1399-01-01", close_price=7, volume=100,
    )

    with patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_daily_history",
        return_value=HISTORY,
    ), patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_candlesticks",
        return_value={},
    ):
        call_command("resync_symbol_from_provider", "کاما", apply=True,
                     force_during_session=True)

    assert MarketCandle.objects.filter(
        timeframe=MarketCandle.ADJUSTED, date_time="1399-01-01"
    ).exists()


def test_daily_history_is_brought_up_to_the_provider():
    _run(apply=True)

    rows = DailyStockHistory.objects.filter(symbol="کاما")
    assert set(rows.values_list("date", flat=True)) == {"1405-05-11", "1405-05-12"}


def test_a_truncated_second_fetch_aborts_instead_of_deleting():
    """A short response is indistinguishable from a shrunken history.

    One flaky call returned 4,506 of 4,509 adjusted rows. Acting on it would have
    deleted three years of real prices, so two fetches must agree first.
    """
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-11", close_price=105, volume=5000,
    )

    with patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_daily_history",
        side_effect=[HISTORY, HISTORY[:1]],
    ), patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_candlesticks",
        return_value=ADJUSTED,
    ):
        with pytest.raises(CommandError, match="truncated"):
            call_command("resync_symbol_from_provider", "کاما", apply=True,
                     force_during_session=True)

    assert MarketCandle.objects.filter(date_time="1405-05-11").exists()


def test_apply_refuses_while_the_session_is_open():
    """The provider serves the day mid-write; consecutive calls disagree."""
    with patch(
        "marketdata.management.commands.resync_symbol_from_provider.market_state."
        "market_state", return_value="open"
    ):
        with pytest.raises(CommandError, match="session is open"):
            call_command("resync_symbol_from_provider", "کاما", apply=True)

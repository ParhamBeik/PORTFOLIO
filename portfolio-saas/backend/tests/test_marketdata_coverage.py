"""Tests for per-day tick coverage and the live-quota reserve that funds it.

Two behaviours the archive gained together:

- Which days are worth spending a request on. Requesting ticks for a day the
  exchange was shut costs a request and returns nothing, and no local weekday
  rule knows Iranian public holidays -- so the calendar is read out of the candle
  table, where a holiday shows up as no symbol quoting at all.
- How many requests the archive may take. The old static floor reserved the same
  600 for live prices at 23:00 as at 08:00, so the tail of a quiet day went
  unspent; the reserve now shrinks as the day closes.

The calendar helpers query two tables, so they are narrow integration tests. The
reserve is arithmetic over a settings object and one row, so it is a unit test.
"""
from datetime import datetime, timezone as dt_timezone

import pytest

from marketdata import jalali, quota
from marketdata.archive import (
    _tick_dates_needed,
    _tick_days_unreconciled,
    market_trading_days,
)
from marketdata.models import ApiRequestQuota, MarketCandle, StockTransactionTick


def _candles(day, count, *, volume=1000):
    MarketCandle.objects.bulk_create([
        MarketCandle(
            symbol=f"SYM{i}", timeframe="1d_unadj", date_time=day,
            open_price=100, high_price=110, low_price=95, close_price=105,
            volume=volume,
        )
        for i in range(count)
    ])


@pytest.mark.django_db
class TestTradingCalendar:
    def test_a_day_the_whole_market_quoted_is_a_trading_day(self):
        day = jalali.today()
        _candles(day, 50)
        assert market_trading_days() == {day}

    def test_a_holiday_with_a_few_stray_prints_is_not_a_trading_day(self):
        """Late corrections leave a handful of candles on a closed day."""
        recent = jalali.recent_days(5)
        _candles(recent[0], 50)   # a real session
        _candles(recent[1], 3)    # 3 of 50 symbols: the exchange was shut
        assert market_trading_days() == {recent[0]}

    def test_days_outside_the_window_are_never_offered(self):
        recent = jalali.recent_days(5)
        _candles(recent[0], 50)
        _candles(recent[4], 50)
        assert market_trading_days(window_days=2) == {recent[0]}

    def test_an_empty_candle_table_yields_no_days_rather_than_every_day(self):
        assert market_trading_days() == set()


@pytest.mark.django_db
class TestTickCoverage:
    def test_a_day_whose_ticks_add_up_is_left_alone(self):
        day = jalali.today()
        _candles(day, 50, volume=300)
        StockTransactionTick.objects.create(
            symbol="SYM0", date=day, row=1, time="09:15:00",
            price=100, volume=300, canceled=False,
        )
        assert _tick_days_unreconciled("SYM0", {day}) == set()
        assert _tick_dates_needed("SYM0") == []

    def test_a_day_missing_ticks_entirely_is_queued(self):
        day = jalali.today()
        _candles(day, 50)
        assert _tick_dates_needed("SYM0") == [day]

    def test_a_day_whose_ticks_do_not_add_up_is_queued_for_repair(self):
        """Stored-but-wrong is the case a row counter can never see."""
        day = jalali.today()
        _candles(day, 50, volume=300)
        StockTransactionTick.objects.create(
            symbol="SYM0", date=day, row=1, time="09:15:00",
            price=100, volume=180, canceled=False,  # 120 short of the candle
        )
        assert _tick_days_unreconciled("SYM0", {day}) == {day}
        assert _tick_dates_needed("SYM0") == [day]

    def test_cancelled_trades_do_not_count_toward_the_day(self):
        day = jalali.today()
        _candles(day, 50, volume=300)
        StockTransactionTick.objects.bulk_create([
            StockTransactionTick(symbol="SYM0", date=day, row=1, time="09:15:00",
                                 price=100, volume=300, canceled=False),
            StockTransactionTick(symbol="SYM0", date=day, row=2, time="09:20:00",
                                 price=100, volume=500, canceled=True),
            # The cancellation twin: same row, reported again at cancellation time.
            StockTransactionTick(symbol="SYM0", date=day, row=2, time="12:40:00",
                                 price=100, volume=500, canceled=True),
        ])
        assert _tick_days_unreconciled("SYM0", {day}) == set()

    def test_the_cancellation_twin_survives_the_widened_unique_key(self):
        """Keying on row alone dropped ~8% of a busy day; time is part of the key."""
        day = jalali.today()
        StockTransactionTick.objects.bulk_create([
            StockTransactionTick(symbol="SYM0", date=day, row=2, time="09:20:00",
                                 price=100, volume=500, canceled=True),
            StockTransactionTick(symbol="SYM0", date=day, row=2, time="12:40:00",
                                 price=100, volume=500, canceled=True),
        ], ignore_conflicts=True)
        assert StockTransactionTick.objects.filter(symbol="SYM0", row=2).count() == 2


@pytest.mark.django_db
class TestLiveReserve:
    """How much of the day's quota the archive must leave for live prices."""

    def _row(self, **over):
        fields = {"day": quota.quota_day(), "limit": 9800, "used": 0,
                  "live_used": 0, "archive_used": 0, "other_used": 0}
        fields.update(over)
        return ApiRequestQuota(**fields)

    def test_a_full_day_ahead_reserves_every_cycle_it_will_need(self, settings):
        """288 five-minute cycles x 2 calls = 576, the figure the cadence implies."""
        settings.MARKETDATA_LIVE_REQUEST_FLOOR = 600
        settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 200
        settings.MARKETDATA_QUOTA_TIMEZONE = "Asia/Tehran"
        # 00:00 Tehran is 20:30 UTC the previous day: a whole quota day remains.
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        assert quota.live_reserve_remaining(self._row(), now=midnight_tehran) == 576

    def test_the_reserve_shrinks_as_the_day_closes(self, settings):
        settings.MARKETDATA_LIVE_REQUEST_FLOOR = 600
        settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 200
        settings.MARKETDATA_QUOTA_TIMEZONE = "Asia/Tehran"
        # 22:00 Tehran: two hours left = 24 cycles = 48 requests.
        two_hours_left = datetime(2026, 7, 27, 18, 30, tzinfo=dt_timezone.utc)
        assert quota.live_reserve_remaining(self._row(), now=two_hours_left) == 48

    def test_the_reserve_never_exceeds_what_live_could_still_spend(self, settings):
        """Live cannot borrow, so holding more than its bucket protects nothing."""
        settings.MARKETDATA_LIVE_REQUEST_FLOOR = 100
        settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
        settings.MARKETDATA_QUOTA_TIMEZONE = "Asia/Tehran"
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        row = self._row(live_used=70)
        assert quota.live_reserve_remaining(row, now=midnight_tehran) == 30

    def test_a_faster_cadence_reserves_more(self, settings):
        settings.MARKETDATA_LIVE_REQUEST_FLOOR = 6000
        settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
        settings.MARKETDATA_LIVE_INTERVAL_OPEN = 120  # the old 2-minute loop
        settings.MARKETDATA_QUOTA_TIMEZONE = "Asia/Tehran"
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        assert quota.live_reserve_remaining(self._row(), now=midnight_tehran) == 1440

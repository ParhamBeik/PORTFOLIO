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
from unittest.mock import patch

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

    def test_failed_tick_replacement_retains_the_previous_day(self, settings):
        from unittest.mock import patch
        from marketdata.archive import run_archive_state
        from marketdata.models import ArchiveFetchState

        settings.TSETMC_API_KEY = "test-key"
        day = jalali.today()
        _candles(day, 50, volume=100)
        old = StockTransactionTick.objects.create(
            symbol="SYM0", date=day, row=1, time="09:15:00",
            price=100, volume=50,
        )
        state = ArchiveFetchState.objects.create(
            endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
            symbol="SYM0",
        )
        replacement = [{
            "row": 2, "time": "09:20:00", "price": 101,
            "volume": 60, "date": day,
        }]
        with patch("marketdata.archive.fetch_transactions", return_value=replacement):
            run_archive_state(state.pk)

        assert StockTransactionTick.objects.filter(pk=old.pk, volume=50).exists()
        assert not StockTransactionTick.objects.filter(symbol="SYM0", row=2).exists()


@pytest.mark.django_db
class TestLiveReserve:
    """How much of the day's quota the archive must leave for live prices."""

    def _row(self, **over):
        fields = {"day": quota.quota_day(), "limit": 9800, "used": 0,
                  "live_used": 0, "archive_used": 0, "other_used": 0}
        fields.update(over)
        return ApiRequestQuota(**fields)

    @staticmethod
    def _configure(settings, *, tse=False):
        settings.BRS_API_KEY = "brs-key"
        settings.TSETMC_API_KEY = "tse-key" if tse else ""
        settings.MARKETDATA_LIVE_INTERVAL_OPEN = 300
        settings.MARKETDATA_LIVE_INTERVAL_DAYTIME = 300
        settings.MARKETDATA_LIVE_INTERVAL_OVERNIGHT = 300
        settings.MARKETDATA_LIVE_REQUEST_FLOOR = 6000
        settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
        settings.MARKETDATA_QUOTA_TIMEZONE = "Asia/Tehran"
        settings.MARKETDATA_IGNORE_MARKET_HOURS = False

    def test_a_full_day_ahead_reserves_every_cycle_it_will_need(self, settings):
        """Reserve the real BRS plan, including Monday's commodity opening."""
        self._configure(settings)
        # 00:00 Tehran is 20:30 UTC the previous day: a whole quota day remains.
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        assert quota.live_reserve_remaining(self._row(), now=midnight_tehran) == 738

    def test_the_reserve_shrinks_as_the_day_closes(self, settings):
        self._configure(settings)
        # 22:00-23:00 has gold+crypto+commodity; 23:00-00:00 has two jobs.
        two_hours_left = datetime(2026, 7, 27, 18, 30, tzinfo=dt_timezone.utc)
        assert quota.live_reserve_remaining(self._row(), now=two_hours_left) == 60

    def test_the_reserve_never_exceeds_what_live_could_still_spend(self, settings):
        """Live cannot borrow, so holding more than its bucket protects nothing."""
        self._configure(settings)
        settings.MARKETDATA_LIVE_REQUEST_FLOOR = 100
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        row = self._row(live_used=70)
        assert quota.live_reserve_remaining(row, now=midnight_tehran) == 30

    def test_a_faster_cadence_reserves_more(self, settings):
        """But only for the hours the fast cadence actually runs.

        The open-market interval applies 08:30-13:00 on a trading day, not all
        24 hours. Costing the whole day at it reserved ~4,300/day against an
        observed live spend of 24-719/day and locked the archive out of the tail
        of every day, so the reserve is now priced per market state.
        """
        self._configure(settings, tse=True)
        # 1405-05-05, a Monday: the session runs, so the open interval bites.
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)

        settings.MARKETDATA_LIVE_INTERVAL_OPEN = 300
        baseline = quota.live_reserve_remaining(self._row(), now=midnight_tehran)
        settings.MARKETDATA_LIVE_INTERVAL_OPEN = 120  # the old 2-minute loop
        faster = quota.live_reserve_remaining(self._row(), now=midnight_tehran)

        assert faster > baseline
        # BRS costs 576 off-session; six jobs run during the 135 open cycles,
        # plus one provider-state probe every 30 minutes of the 4.5h session.
        assert faster == 576 + 6 * 135 + 9
        assert faster < 6 * 720

    def test_the_session_cadence_is_not_charged_on_a_closed_day(self, settings):
        """Thursday/Friday is the Iranian weekend; no session runs to pay for."""
        self._configure(settings, tse=True)
        settings.MARKETDATA_LIVE_INTERVAL_OPEN = 120
        # 1405-05-09 is a Friday (jdatetime weekday 6).
        friday_midnight = datetime(2026, 7, 30, 20, 30, tzinfo=dt_timezone.utc)
        assert quota.live_reserve_remaining(self._row(), now=friday_midnight) == 768

    def test_no_credentials_means_no_phantom_reserve(self, settings):
        self._configure(settings)
        settings.BRS_API_KEY = ""
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        assert quota.live_reserve_remaining(self._row(), now=midnight_tehran) == 0


def test_live_job_plan_changes_with_market_state():
    from marketdata.market_state import CLOSED_DAYTIME, OPEN, OVERNIGHT, live_job_keys

    monday_noon = datetime(2026, 7, 27, 8, 30, tzinfo=dt_timezone.utc)
    assert live_job_keys(
        state=OPEN, now=monday_noon, has_brs=True, has_tsetmc=True
    ) == (
        "gold_currency", "crypto", "commodity",
        "tsetmc", "option_contracts", "etf_nav",
    )
    assert live_job_keys(
        state=CLOSED_DAYTIME, now=monday_noon, has_brs=True, has_tsetmc=True
    ) == ("gold_currency", "crypto", "commodity")
    assert live_job_keys(
        state=OVERNIGHT, now=monday_noon, has_brs=True, has_tsetmc=True
    ) == ("crypto", "commodity")


def test_index_probe_closes_a_holiday_before_tse_jobs(settings):
    from marketdata.market_state import CLOSED_DAYTIME
    from portfolio.live.fetcher import fetch_all_markets

    settings.MARKETDATA_IGNORE_MARKET_HOURS = False
    settings.TSETMC_SYMBOL_URL = "https://example.test/symbol"
    index_payload = {"date": "1405-05-17", "time": "08:30", "state": "بسته"}
    with (
        patch("marketdata.market_state.claim_provider_state_probe", return_value=True),
        patch("marketdata.fetchers.index.fetch_market_index", return_value=index_payload) as index,
        patch("marketdata.ingest.ingest_market_index", return_value=(1, 0)),
        patch("marketdata.market_state.market_state", return_value=CLOSED_DAYTIME),
        patch("portfolio.live.fetcher._tsetmc_job") as stocks,
        patch("portfolio.live.fetcher._option_job") as options,
        patch("portfolio.live.fetcher._etf_nav_job") as etfs,
    ):
        raw = fetch_all_markets({
            "brs_url": "", "brs_api_key": "",
            "tsetmc_url": "https://example.test/tse", "tsetmc_api_key": "key",
            "tsetmc_symbol_url": "https://example.test/symbol",
        })

    index.assert_called_once_with("key")
    assert raw["market_index"] == index_payload
    stocks.assert_not_called()
    options.assert_not_called()
    etfs.assert_not_called()

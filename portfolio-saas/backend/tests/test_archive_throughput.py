from unittest.mock import patch

import pytest
import requests

from marketdata.archive import _tick_trading_days, claim_archive_batch
from marketdata.fetchers.base import TransientMarketDataError, fetch_json
from marketdata.models import ArchiveFetchState, MarketCandle
from marketdata.quota import ARCHIVE

pytestmark = pytest.mark.django_db


def test_batch_reserves_two_non_tick_and_uses_ten_tick_slots():
    non_ticks = [
        ArchiveFetchState.objects.create(
            endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
            symbol=f"repair-{index}",
        )
        for index in range(3)
    ]
    ticks = [
        ArchiveFetchState.objects.create(
            endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
            symbol=f"tick-{index}",
            stored_rows=index,
        )
        for index in range(12)
    ]

    claimed = claim_archive_batch(limit=12)

    assert len(set(claimed) & {row.pk for row in non_ticks}) == 2
    assert len(set(claimed) & {row.pk for row in ticks}) == 10
    claimed_ticks = [row.pk for row in ticks[:10]]
    assert set(claimed_ticks).issubset(claimed)


def test_completed_state_is_not_claimed_by_normal_batch():
    complete = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="complete",
        verified_complete=True,
    )
    assert complete.pk not in claim_archive_batch(limit=12)


def test_tick_calendar_accepts_a_symbol_with_only_adjusted_candles():
    """35 live symbols have thousands of adjusted bars and zero unadjusted ones.

    Reading only `1d_unadj` left their trading calendar empty, so the tick state
    raised "no trading days known" forever instead of ever fetching a tick.
    """
    from django.core.cache import cache

    from marketdata import jalali

    # The calendar is derived from data inside a trailing window, so anchor the
    # fixture to real recent days rather than hard-coded ones that age out.
    cache.clear()
    days = sorted(jalali.recent_days(90))[-3:]
    # A market-wide calendar needs other symbols trading on the same days.
    for peer in range(5):
        for day in days:
            MarketCandle.objects.create(
                symbol=f"peer-{peer}",
                timeframe=MarketCandle.UNADJUSTED,
                date_time=day,
                close_price=100,
                volume=10,
            )
    for day in days:
        MarketCandle.objects.create(
            symbol="adj-only",
            timeframe=MarketCandle.ADJUSTED,
            date_time=day,
            close_price=100,
            volume=10,
        )

    assert _tick_trading_days("adj-only", window_days=90) == set(days)
    assert _tick_trading_days("never-fetched", window_days=90) == set()


def test_volume_mismatch_is_recorded_so_the_day_can_be_forgiven():
    """A day the provider never serves consistently is a gap, not a retry.

    Nothing recorded the mismatch, so 24 tick states re-fetched the same broken
    day forever and none could reach complete -- which also froze the 90->365 day
    promotion, since that requires every tick state to be complete.
    """
    from marketdata import ingest, validation
    from marketdata.archive import _fetch_and_ingest
    from marketdata.models import RejectedRecord

    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS, symbol="MM"
    )
    day = "1405-05-11"
    with patch("marketdata.archive._tick_dates_needed", return_value=[day]), patch(
        "marketdata.archive.fetch_transactions", return_value=[{"volume": 5}]
    ), patch.object(ingest, "screen", return_value=([{"volume": 5}], 0)), patch.object(
        validation, "reconcile_tick_volume", return_value="tick_volume_mismatch:5!=99"
    ), patch("marketdata.archive._symbol_candle_volumes", return_value={day: 99}):
        with pytest.raises(Exception, match="tick_volume_mismatch"):
            _fetch_and_ingest(state)

    record = RejectedRecord.objects.get(endpoint="stock_transaction_ticks", symbol="MM")
    assert record.date == day
    assert record.reason == "tick_volume_mismatch"
    assert "5!=99" in record.payload["detail"]


def test_archive_fetch_has_one_physical_attempt_by_default():
    with patch("marketdata.fetchers.base.reserve_request") as reserve, patch(
        "marketdata.fetchers.base.requests.get", side_effect=requests.Timeout("timeout")
    ):
        with pytest.raises(TransientMarketDataError):
            fetch_json("https://example.test", quota_bucket=ARCHIVE)
    assert reserve.call_count == 1

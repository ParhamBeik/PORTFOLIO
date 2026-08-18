"""marketdata.calendars.is_closure_day / is_contract_expired: the single
dispatch point deciding whether a missing day is a legitimate closure or a
real ingest gap, per asset class. Each branch is a genuinely different data
source (DailyStockHistory volume/trades, GoldCurrencyHistory feed breadth,
MarketSnapshot feed breadth, MarketIndexData's own state field, or -- for
crypto -- no tolerance at all), so each is exercised independently here
rather than only indirectly through the daily-bar aggregator tests.
"""
from decimal import Decimal

import pytest

from marketdata import calendars
from marketdata.models import (
    DailyStockHistory,
    DerivativeContract,
    GoldCurrencyHistory,
    MarketIndexData,
    MarketSnapshot,
)

pytestmark = pytest.mark.django_db


def _stock_day(date, *, volume, trades, symbol="کاما"):
    return DailyStockHistory.objects.create(
        symbol=symbol, date=date, time="12:30",
        tno=trades, tvol=volume, tval=volume * 10,
        py=Decimal("2475"), pl=Decimal("2475"), plc=Decimal("0"),
        plp=0.0, pc=Decimal("2475"),
    )


@pytest.mark.parametrize("asset_class", ["stock", "tse_option", "etf_nav"])
def test_tse_underlying_classes_share_the_stock_closure_calendar(asset_class):
    _stock_day("1404-06-01", volume=1_000_000, trades=500)
    _stock_day("1404-06-02", volume=0, trades=0)

    assert calendars.is_closure_day(asset_class, "کاما", "1404-06-02") is True
    assert calendars.is_closure_day(asset_class, "کاما", "1404-06-01") is False


def test_stock_closure_with_no_data_at_all_is_not_forgiven():
    # market_closure_days returns an empty set when there is nothing to judge
    # against; that must fail closed (a real gap), not be waved through.
    assert calendars.is_closure_day("stock", "کاما", "1399-01-01") is False


@pytest.mark.parametrize("asset_class", ["gold", "currency"])
def test_gold_and_currency_use_feed_breadth_quoting_days(asset_class):
    date_open = "1404-07-10"
    date_closed = "1404-07-11"
    for i in range(6):
        GoldCurrencyHistory.objects.create(
            symbol=f"SYM{i}", date=date_open, close_price=Decimal("100"),
            unit="Toman",
        )
    # A thin, non-representative day: below MIN_FEED_BREADTH_FOR_CALENDAR (5).
    GoldCurrencyHistory.objects.create(
        symbol="USD", date=date_closed, close_price=Decimal("100"), unit="Toman",
    )

    assert calendars.is_closure_day(asset_class, "USD", date_closed) is True
    assert calendars.is_closure_day(asset_class, "USD", date_open) is False


def test_gold_currency_fails_closed_when_feed_too_thin_to_trust():
    GoldCurrencyHistory.objects.create(
        symbol="USD", date="1404-08-01", close_price=Decimal("100"), unit="Toman",
    )
    # Only one symbol on this date -- below the breadth floor, so the
    # quoting-days set is empty and the day must NOT be treated as a closure.
    assert calendars.is_closure_day("gold", "USD", "1404-08-01") is False


def test_crypto_is_never_forgiven():
    # No data, some data, lots of data -- crypto markets never close, so this
    # must always return False, unlike every other branch.
    assert calendars.is_closure_day("crypto", "BTC", "1404-01-01") is False
    for i in range(50):
        MarketSnapshot.objects.create(
            asset_class="crypto", symbol=f"COIN{i}",
            observed_at="2025-01-01T00:00:00Z", last_price=Decimal("1"),
        )
    assert calendars.is_closure_day("crypto", "BTC", "1404-01-01") is False


@pytest.mark.parametrize("asset_class", ["commodity", "ime_future", "ime_option"])
def test_commodity_and_derivative_classes_use_snapshot_breadth(asset_class):
    from marketdata import jalali

    open_jalali = "1404-12-11"
    closed_jalali = "1404-12-12"
    open_day = jalali.to_datetime(open_jalali)
    closed_day = jalali.to_datetime(closed_jalali)
    for i in range(4):
        MarketSnapshot.objects.create(
            asset_class=asset_class, symbol=f"SYM{i}",
            observed_at=open_day, last_price=Decimal("100"),
        )
    # Below _MIN_BREADTH_FOR_CALENDAR (3): a thin day, not a representative one.
    MarketSnapshot.objects.create(
        asset_class=asset_class, symbol="SYM0",
        observed_at=closed_day, last_price=Decimal("100"),
    )

    assert calendars.is_closure_day(asset_class, "SYM0", closed_jalali) is True
    assert calendars.is_closure_day(asset_class, "SYM0", open_jalali) is False


def test_index_reads_provider_closed_state():
    from marketdata.market_state import PROVIDER_CLOSED

    MarketIndexData.objects.create(date="1404-09-01", time="09:00", state=PROVIDER_CLOSED)
    MarketIndexData.objects.create(date="1404-09-02", time="09:00", state="باز")

    assert calendars.is_closure_day("index", "overall", "1404-09-01") is True
    assert calendars.is_closure_day("index", "overall", "1404-09-02") is False


def test_index_with_no_state_recorded_is_not_forgiven():
    assert calendars.is_closure_day("index", "overall", "1404-09-03") is False


def test_unknown_asset_class_is_never_forgiven():
    assert calendars.is_closure_day("nonexistent_class", "X", "1404-01-01") is False


def test_contract_expired_true_past_expiry():
    DerivativeContract.objects.create(
        kind="tse_option", contract_code="OPT1", expiry_date="1404-01-01",
    )
    assert calendars.is_contract_expired("tse_option", "OPT1", "1404-02-01") is True
    assert calendars.is_contract_expired("tse_option", "OPT1", "1403-12-01") is False


def test_contract_expired_false_when_no_expiry_on_file():
    DerivativeContract.objects.create(kind="tse_option", contract_code="OPT2")
    assert calendars.is_contract_expired("tse_option", "OPT2", "1404-02-01") is False


def test_contract_expired_false_for_unknown_contract():
    assert calendars.is_contract_expired("tse_option", "GHOST", "1404-02-01") is False

"""MarketSnapshot/MarketDailyBar: the unified live->historical mechanism for
crypto, commodity, ETF NAV, and market index (see marketdata/calendars.py and
marketdata/ingest.py's aggregate_market_daily_bars)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from marketdata import ingest, jalali
from marketdata.models import MarketDailyBar, MarketIndexData, MarketSnapshot

pytestmark = pytest.mark.django_db


def test_ingest_market_snapshots_flattens_dict_of_lists_payload():
    payload = {
        "metal_precious": [{"symbol": "XAUUSD", "price": "2400.5"}],
        "energy": [{"symbol": "WTI", "price": "80.1"}],
    }
    created, skipped = ingest.ingest_market_snapshots("commodity", payload)

    assert created == 2
    assert skipped == 0
    assert set(MarketSnapshot.objects.values_list("symbol", flat=True)) == {"XAUUSD", "WTI"}


def test_aggregate_market_daily_bars_builds_ohlc_from_snapshots():
    date = jalali.today()
    start = jalali.to_datetime(date)
    for offset_minutes, price in ((0, "100"), (30, "110"), (60, "90"), (90, "105")):
        MarketSnapshot.objects.create(
            asset_class="crypto",
            symbol="BTC",
            observed_at=start + timedelta(minutes=offset_minutes),
            last_price=price,
            volume=10,
        )

    created, skipped = ingest.aggregate_market_daily_bars("crypto", date)

    assert created == 1
    assert skipped == 0
    bar = MarketDailyBar.objects.get(asset_class="crypto", symbol="BTC", date=date)
    assert bar.open_price == 100
    assert bar.high_price == 110
    assert bar.low_price == 90
    assert bar.close_price == 105
    assert bar.sample_count == 4


def test_crypto_missing_day_is_a_real_gap_never_forgiven():
    """Crypto never closes -- a symbol with zero snapshots for the day must
    not be silently absorbed into the 'skipped' (legitimate closure) count."""
    date = jalali.today()

    created, skipped = ingest.aggregate_market_daily_bars("crypto", date, symbols=["BTC"])

    assert created == 0
    assert skipped == 0
    assert not MarketDailyBar.objects.filter(asset_class="crypto", symbol="BTC").exists()


def test_aggregate_market_daily_bars_reads_index_from_market_index_data():
    date = jalali.today()
    MarketIndexData.objects.create(
        date=date, time="09:00", index_overall=100, index_equal_weight=50, trade_volume=1000
    )
    MarketIndexData.objects.create(
        date=date, time="12:30", index_overall=105, index_equal_weight=48, trade_volume=500
    )

    created, _skipped = ingest.aggregate_market_daily_bars("index", date)

    assert created == 2
    overall = MarketDailyBar.objects.get(asset_class="index", symbol="overall", date=date)
    assert overall.open_price == 100
    assert overall.close_price == 105
    assert overall.high_price == 105
    equal_weight = MarketDailyBar.objects.get(asset_class="index", symbol="equal_weight", date=date)
    assert equal_weight.open_price == 50
    assert equal_weight.close_price == 48

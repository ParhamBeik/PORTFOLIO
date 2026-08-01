import pytest
from django.core.management import call_command

from marketdata.models import MarketCandle, RejectedRecord


pytestmark = pytest.mark.django_db


def test_cleanup_quarantines_only_when_applied():
    candle = MarketCandle.objects.create(
        symbol="TEST", timeframe="1d_adj", date_time="1405-05-03",
        open_price=100, high_price=90, low_price=95, close_price=96, volume=1,
    )
    call_command("clean_invalid_candles")
    assert MarketCandle.objects.filter(pk=candle.pk).exists()

    call_command("clean_invalid_candles", "--apply")
    assert not MarketCandle.objects.filter(pk=candle.pk).exists()
    assert RejectedRecord.objects.filter(
        endpoint="stock_candle_adjusted", symbol="TEST", reason="high_below_low"
    ).exists()

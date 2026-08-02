from django.db.models import Exists, F, OuterRef, Q

from .models import MarketCandle


def candle_close_qs(symbol, *, as_of=None):
    """Daily candles with provider-adjusted rows preferred per symbol and day."""
    adjusted = MarketCandle.objects.filter(
        symbol=OuterRef("symbol"),
        date_time=OuterRef("date_time"),
        timeframe=MarketCandle.ADJUSTED,
    )
    queryset = MarketCandle.objects.filter(
        timeframe__in=(MarketCandle.ADJUSTED, MarketCandle.AGGREGATE),
        close_price__gt=0,
    ).annotate(
        has_adjusted=Exists(adjusted),
        candle_source=F("timeframe"),
    ).filter(
        Q(timeframe=MarketCandle.ADJUSTED)
        | Q(timeframe=MarketCandle.AGGREGATE, has_adjusted=False)
    )
    if isinstance(symbol, str):
        queryset = queryset.filter(symbol=symbol)
    else:
        queryset = queryset.filter(symbol__in=symbol)
    if as_of is not None:
        queryset = queryset.filter(date_time__lte=as_of)
    return queryset

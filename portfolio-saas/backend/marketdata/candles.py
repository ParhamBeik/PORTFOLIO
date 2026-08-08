from django.db.models import Count, Exists, F, OuterRef, Q
from django.core.cache import cache

from .models import MarketCandle


def actual_trading_days(*, start=None, end=None, window_days=None):
    """Jalali days when a meaningful share of the TSE universe traded."""
    cache_key = f"marketdata:trading-days:{start}:{end}:{window_days}"
    cached = cache.get(cache_key)
    if cached is not None:
        return set(cached)

    queryset = MarketCandle.objects.filter(timeframe=MarketCandle.UNADJUSTED)
    if start is not None:
        queryset = queryset.filter(date_time__gte=start)
    if end is not None:
        queryset = queryset.filter(date_time__lte=end)
    if window_days is not None:
        from . import jalali
        queryset = queryset.filter(date_time__in=jalali.recent_days(window_days))
    counts = list(
        queryset.values("date_time").annotate(n=Count("symbol", distinct=True))
    )
    if not counts:
        return set()
    floor = max(1, max(row["n"] for row in counts) // 5)
    result = {row["date_time"] for row in counts if row["n"] >= floor}
    cache.set(cache_key, sorted(result), timeout=60 * 60)
    return result


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
        # Include both "1405-05-09" and "1405-05-09 00:00:00" formats for the same day.
        # String comparison: "1405-05-09 00:00:00" > "1405-05-09" lexicographically,
        # so we extend the bound to end-of-day to capture both formats.
        queryset = queryset.filter(date_time__lte=as_of + " 23:59:59")
    return queryset

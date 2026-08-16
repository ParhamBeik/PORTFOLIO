from django.db.models import Count, Exists, F, OuterRef, Q, Sum
from django.core.cache import cache

from .models import DailyStockHistory, MarketCandle

# Fewest distinct gold/FX symbols on a day before that feed's own publishing
# pattern is trusted as a market calendar (see gold_currency_quoting_days).
MIN_FEED_BREADTH_FOR_CALENDAR = 5


def market_closure_days(*, start=None, end=None) -> set[str]:
    """Jalali days the whole exchange was shut, distinguished from ingest holes.

    The provider keeps emitting a `DailyStockHistory` row for every symbol on a
    closed day, carrying the last known price with **zero volume and zero
    trades**. That padding is what makes a closure indistinguishable from a
    warehouse gap if you only look at whether rows exist: `MarketCandle` (which
    feeds the returns matrix) correctly has nothing for those days, so the
    series looks broken even though no data is missing.

    A day where the summed volume AND trade count across the entire market are
    both zero is a closure, not a failure to fetch. Verified against کاما in
    Esfand 1404: 1404-12-06 traded 36.4M shares over 992 trades, then every day
    through 1405-02-28 reports volume 0 / trades 0 at a frozen price of 2475.

    Historically 74 such days across 12 Jalali years — mostly one-to-three-day
    national holidays, plus the extended 1404-1405 closure.
    """
    cache_key = f"marketdata:closure-days:{start}:{end}"
    cached = cache.get(cache_key)
    if cached is not None:
        return set(cached)

    queryset = DailyStockHistory.objects.all()
    if start is not None:
        queryset = queryset.filter(date__gte=start)
    if end is not None:
        queryset = queryset.filter(date__lte=end)
    rows = queryset.values("date").annotate(vol=Sum("tvol"), trades=Sum("tno"))
    result = {r["date"] for r in rows if not r["vol"] and not r["trades"]}
    cache.set(cache_key, sorted(result), timeout=60 * 60)
    return result


def gold_currency_quoting_days(*, start=None, end=None) -> set[str]:
    """Jalali days the gold/FX feed broadly published, for integrity coverage.

    Not every symbol on this feed quotes every day: USD, gold and USDT print
    seven days a week, while EUR/GBP/CHF/CAD skip Fridays and public holidays.
    Measuring coverage against "every calendar day" therefore fails the six-day
    symbols for a gap they do not have -- EUR scored 150/180 = 0.833 against a
    0.90 gate and was dropped from the optimizer universe as `low_coverage`.

    Observed breadth: 31-38 symbols on an ordinary day, 9-11 on a Friday. A
    minority-quoting day is a market holiday for most of the feed, not missing
    data, so it is excluded from the expected set. Mirrors the fifth-of-peak
    floor `actual_trading_days` already uses for the TSE calendar.
    """
    from .models import GoldCurrencyHistory

    cache_key = f"marketdata:fx-quoting-days:{start}:{end}"
    cached = cache.get(cache_key)
    if cached is not None:
        return set(cached)

    queryset = GoldCurrencyHistory.objects.all()
    if start is not None:
        queryset = queryset.filter(date__gte=start)
    if end is not None:
        queryset = queryset.filter(date__lte=end)
    counts = list(queryset.values("date").annotate(n=Count("symbol", distinct=True)))
    if not counts:
        return set()
    # Inferring a market calendar from the feed is circular unless the feed is
    # broad enough to be representative: with one or two symbols every day it
    # happens to cover scores 100% and a genuine gap disappears. Below that bar
    # return nothing, so the caller falls back to the conservative
    # every-calendar-day expectation. Production carries ~38 symbols.
    if max(row["n"] for row in counts) < MIN_FEED_BREADTH_FOR_CALENDAR:
        return set()
    floor = max(1, max(row["n"] for row in counts) // 2)
    result = {row["date"] for row in counts if row["n"] >= floor}
    cache.set(cache_key, sorted(result), timeout=60 * 60)
    return result


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

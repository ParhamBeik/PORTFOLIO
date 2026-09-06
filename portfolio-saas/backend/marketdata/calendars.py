"""Which days a market was actually open, and therefore which gaps are real.

Two layers, in this order:

* The raw calendars, each derived from the warehouse rather than hardcoded --
  `market_closure_days` (TSE), `gold_currency_quoting_days` (gold/FX),
  `actual_trading_days`, plus `candle_close_qs`, the one blessed reader for
  adjusted closes.
* `is_closure_day` / `is_contract_expired`, the single dispatch point that maps
  an asset class onto the right calendar. Crypto never closes at all, so it gets
  no forgiveness -- a missing day is always a gap; commodities follow a global
  calendar unrelated to TSE's Thursday/Friday weekend; and a derivative's own
  expiry is a "nothing to fetch" reason distinct from a market closure.

Getting this wrong in either direction is a real failure mode: too lenient hides
an ingest hole, too strict flags every weekend as broken.
"""

from bisect import bisect_right

from django.db.models import Count, Sum
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


def symbol_halt_days(symbol, *, start=None, end=None) -> set[str]:
    """Jalali days this ONE symbol was halted while the exchange itself traded.

    The market-wide twin of this check is `market_closure_days` above, and the
    provider's behaviour is identical in both cases: a symbol that did not trade
    still gets a `DailyStockHistory` row carrying its last known price with zero
    volume and zero trades. Market-wide that means a closure; for a single symbol
    on a day the rest of the market traded, it means a trading halt.

    This matters because `MarketCandle` -- what the returns matrix and the
    integrity gate actually read -- correctly has nothing for those days. Without
    this the gate scores a suspended stock as having a data gap and drops it from
    the universe, which is a corruption verdict on a symbol whose data is fine.
    The exchange simply did not print a price, and no amount of backfill will
    ever produce one.
    """
    queryset = DailyStockHistory.objects.filter(symbol=symbol)
    if start is not None:
        queryset = queryset.filter(date__gte=start)
    if end is not None:
        queryset = queryset.filter(date__lte=end)
    quiet = {
        row["date"]
        for row in queryset.values("date").annotate(vol=Sum("tvol"), trades=Sum("tno"))
        if not row["vol"] and not row["trades"]
    }
    # Subtract the days the whole exchange was shut: those are already excluded
    # from the expected session set, and double-counting them here would let a
    # genuinely missing day hide behind a closure it had nothing to do with.
    return quiet - market_closure_days(start=start, end=end)


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


def session_calendar(market: str, *, start: str, end: str) -> list[str]:
    """Sorted Jalali days the given market held a session, within [start, end].

    Deliberately the raw distinct-date set, not the breadth-gated
    `actual_trading_days`/`gold_currency_quoting_days`. Those exist to score
    coverage, where a thin feed defining its own calendar is circular. Here a
    single symbol printing is proof the market was open, and gating on breadth
    would silently report "not stale" for any window the gate cannot vouch for
    -- failing open on exactly the question this calendar is asked to answer.
    """
    if market == "tse":
        queryset = MarketCandle.objects.filter(timeframe=MarketCandle.ADJUSTED)
        field = "date_time"
    elif market == "gold_currency":
        from .models import GoldCurrencyHistory

        queryset = GoldCurrencyHistory.objects.all()
        field = "date"
    else:
        raise ValueError(f"unknown market calendar: {market!r}")
    # `date_time` carries both "1405-05-09" and "1405-05-09 00:00:00"; the upper
    # bound is extended to end-of-day for the same reason candle_close_qs does
    # it, and the day is taken from the first 10 chars so the two spellings of
    # one session are not counted as two.
    days = (
        queryset.filter(**{f"{field}__gte": start, f"{field}__lte": end + " 23:59:59"})
        .values_list(field, flat=True)
        .distinct()
    )
    return sorted({str(day)[:10] for day in days})


def sessions_between(
    last_date: str, as_of: str, *, market: str | None = None, calendar=None
) -> int:
    """Market sessions strictly after `last_date` and up to `as_of`, inclusive.

    The one blessed answer to "how stale is this price?", shared by every
    forward-fill bound in the codebase. Both Jalali `YYYY-MM-DD` strings.

    The calendar has to be market-wide. Deriving it from the one symbol being
    valued is circular -- that symbol's own latest row is by construction the
    newest one at or before `as_of`, so nothing is ever counted after it and the
    answer is always 0. Counting raw calendar days instead is the opposite
    error: the Thursday/Friday weekend and a public holiday burn days without
    burning sessions, so a five-session bound fires after three real sessions.
    Both mistakes shipped simultaneously before this helper existed.

    Pass `calendar` (from `session_calendar`) when asking repeatedly over one
    window -- the chart walks 90 days per asset and would otherwise issue a
    query per asset per day.
    """
    if not last_date or not as_of or last_date >= as_of:
        return 0
    if calendar is None:
        calendar = session_calendar(market, start=last_date, end=as_of)
    return bisect_right(calendar, as_of) - bisect_right(calendar, last_date)


def market_for_asset(asset) -> str:
    """Which session calendar an asset's price staleness is measured against."""
    return "tse" if asset.tse_symbol else "gold_currency"


def candle_close_qs(symbol, *, as_of=None, since=None):
    """Provider-adjusted daily candles for a symbol.

    `since` is a Jalali lower bound. Passing it also adds the equivalent bound on
    `ts`, which is what lets a partitioned table skip chunks: every predicate here
    is on the Jalali varchar, and a range scan over a string cannot be used for
    partition pruning. Harmless on a plain table -- `ts` is a pure function of the
    date -- and the difference between planning one chunk and planning all of
    them once measured 106 seconds.

    Only ADJUSTED rows: this is the historical-warehouse read path (archive
    fallback / point-in-time valuation), and it must never blend in same-day
    live-tick-derived data. Live rollups live in `portfolio.models.DailyPriceAverage`.
    """
    queryset = MarketCandle.objects.filter(
        timeframe=MarketCandle.ADJUSTED,
        close_price__gt=0,
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
    if since is not None:
        queryset = queryset.filter(date_time__gte=since)
        from . import jalali

        floor = jalali.to_datetime(since)
        if floor is not None:
            queryset = queryset.filter(ts__gte=floor)
    return queryset


# Breadth floor before a live feed's own publishing pattern is trusted as a
# calendar, mirroring MIN_FEED_BREADTH_FOR_CALENDAR in candles.py -- with only
# one or two symbols reporting, a genuine gap looks like 100% coverage.
_MIN_BREADTH_FOR_CALENDAR = 3


def _snapshot_quoting_days(asset_class: str, *, start=None, end=None) -> set[str]:
    """Jalali days `MarketSnapshot` saw broad activity for one asset class.

    Same shape as `gold_currency_quoting_days`: infer the calendar from the
    feed's own breadth rather than assuming one, since a hardcoded "closed on
    Saturday" guess would be wrong for at least one of these asset classes and
    there is no authoritative published calendar for any of them.

    `start`/`end` are Jalali strings (matching every other date argument in
    this module) but `MarketSnapshot.observed_at` is a real UTC timestamp --
    both the filter bounds and the returned set have to cross that boundary
    explicitly. Bucketing must happen in Tehran-local time, not UTC: Tehran
    midnight for a given Jalali day lands at 20:30 UTC the *previous*
    Gregorian day (fixed +03:30, no DST since 2022), so a plain
    `observed_at__date` (UTC) truncation splits one Tehran day's snapshots
    across two different UTC calendar dates near the boundary -- corrupting
    the breadth count on both sides of it.
    """
    from django.core.cache import cache
    from django.db.models import Count
    from django.db.models.functions import TruncDate

    from . import jalali
    from .models import MarketSnapshot

    cache_key = f"marketdata:snapshot-quoting-days:{asset_class}:{start}:{end}"
    cached = cache.get(cache_key)
    if cached is not None:
        return set(cached)

    queryset = MarketSnapshot.objects.filter(asset_class=asset_class).annotate(
        local_date=TruncDate("observed_at", tzinfo=jalali.TEHRAN)
    )
    if start is not None:
        queryset = queryset.filter(local_date__gte=jalali.to_gregorian(start))
    if end is not None:
        queryset = queryset.filter(local_date__lte=jalali.to_gregorian(end))
    counts = list(
        queryset.values("local_date")
        .annotate(n=Count("symbol", distinct=True))
        .values_list("local_date", "n")
    )
    if not counts:
        return set()
    peak = max(n for _day, n in counts)
    if peak < _MIN_BREADTH_FOR_CALENDAR:
        return set()
    floor = max(1, peak // 2)
    result = {
        jalali.normalize_jalali(day.isoformat()) for day, n in counts if n >= floor
    }
    cache.set(cache_key, sorted(result), timeout=60 * 60)
    return result


def is_closure_day(asset_class: str, symbol: str, date: str) -> bool:
    """True if `date` (Jalali `YYYY-MM-DD`) is a legitimate no-data day.

    `asset_class` matches `MarketSnapshot.AssetClass`/`MarketDailyBar.AssetClass`
    plus `"stock"` for the pre-existing TSE path. `symbol` is accepted for
    forward compatibility (a per-contract calendar, e.g. a specific IME
    contract's own trading hours) but unused by every branch today.
    """
    if asset_class in ("stock", "tse_option", "etf_nav"):
        # TSE-underlying: same exchange, same calendar. ETFs and TSE options
        # trade alongside the stocks they track/settle against.
        return date in market_closure_days(start=date, end=date)

    if asset_class in ("gold", "currency"):
        # `end=date` only, deliberately not `start=date` too: breadth is a
        # RELATIVE measure (this day's symbol count vs. the window's peak),
        # so a single-day window compares a day against itself and can never
        # come out "thin" -- gold_currency_quoting_days would always return
        # either {} (too few rows to judge) or exactly {date} (that day is
        # trivially 100% of its own peak), so `date not in quoting` could
        # never be True. Scanning everything up to `date` gives the
        # comparison real history to judge breadth against; the result is
        # cached by gold_currency_quoting_days itself.
        quoting = gold_currency_quoting_days(end=date)
        # Empty result means the feed was too thin to trust as a calendar
        # (see MIN_FEED_BREADTH_FOR_CALENDAR) -- fail closed, i.e. NOT a
        # forgiven closure, so the caller falls back to treating it as a gap.
        return bool(quoting) and date not in quoting

    if asset_class == "crypto":
        # Crypto markets never close. No tolerance branch: a missing day is
        # always a real gap, never forgiven -- the opposite failure mode from
        # TSE, where over-forgiving would hide a real ingest hole.
        return False

    if asset_class == "commodity":
        # No published calendar to hardcode against (global commodity hours
        # don't align with TSE's Thu/Fri weekend) -- infer it the same
        # way gold/FX does, from the feed's own breadth. Same `end`-only
        # reasoning as the gold/currency branch above -- a single-day window
        # cannot produce a "thin relative to what" signal.
        quoting = _snapshot_quoting_days(asset_class, end=date)
        return bool(quoting) and date not in quoting

    if asset_class == "index":
        from .models import MarketIndexData
        from .market_state import PROVIDER_CLOSED

        return MarketIndexData.objects.filter(date=date).exclude(
            state=""
        ).values_list("state", flat=True).first() == PROVIDER_CLOSED

    return False


def is_contract_expired(kind: str, contract_code: str, date: str) -> bool:
    """True if a derivative contract had already expired by `date`.

    Orthogonal to `is_closure_day`: a dead contract is not a tolerance case,
    it is "there is nothing to fetch," and must not consume retry budget or
    be flagged as a gap in `MarketDailyBar`.
    """
    from .models import DerivativeContract

    contract = DerivativeContract.objects.filter(
        kind=kind, contract_code=contract_code
    ).first()
    if contract is None or not contract.expiry_date:
        return False
    return contract.expiry_date < date

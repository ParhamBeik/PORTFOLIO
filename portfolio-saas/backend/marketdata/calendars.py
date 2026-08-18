"""Single dispatch point for "was this a legitimate no-data day?", per asset class.

Before this module the answer was two independent, hardcoded functions
(`market_closure_days` for TSE, `gold_currency_quoting_days` for gold/FX) with
no path for the asset classes added alongside `MarketSnapshot`/`MarketDailyBar`:
crypto never closes at all, commodities follow a global market calendar with no
relationship to TSE's Thursday/Friday weekend, and a derivative contract's own
expiry is a "nothing to fetch" reason distinct from a market closure. Getting
this wrong in either direction is a real failure mode: too lenient hides a real
ingest gap (which is why crypto gets no forgiveness at all -- it never closes,
so a missing day is always a gap), too strict flags every weekend as broken.
"""
from .candles import gold_currency_quoting_days, market_closure_days

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

    if asset_class in ("commodity", "ime_future", "ime_option"):
        # No published calendar to hardcode against (IME/global commodity
        # hours don't align with TSE's Thu/Fri weekend) -- infer it the same
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

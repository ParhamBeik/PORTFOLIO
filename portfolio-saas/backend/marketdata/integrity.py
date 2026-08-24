"""Windowed integrity assessments for market-history series."""

import datetime as dt
from typing import Any

import jdatetime
from django.utils import timezone

from .calendars import (
    actual_trading_days,
    candle_close_qs,
    gold_currency_quoting_days,
    symbol_halt_days,
)
from .models import (
    GoldCurrencyHistory,
    MarketCandle,
    MarketInstrument,
    RejectedRecord,
    SymbolIntegrity,
)

MAX_FORWARD_FILL_SESSIONS = 5
MIN_COVERAGE = 0.90
MAX_REJECTION_RATIO = 0.01
# A ratio alone is not a usable bar over a ~112-session window: 1% means "at most
# one bad day", so 208 symbols were dropped from the universe over one or two
# rejected rows. Rejected rows are already excluded from the series, so a handful
# is a data-quality note, not grounds to refuse an asset. Both the floor AND the
# ratio must be exceeded.
MIN_REJECTIONS_FOR_GATE = 5
# Separating "we have not fetched this yet" from "this data is broken". Both
# conditions are relative to the window: an absolute row count alone would label
# a legitimately short window as un-backfilled. A symbol holding less than half
# the sessions it should is a gap in OUR warehouse; one holding most of them with
# holes is a gap in the DATA, and only the second deserves a corruption verdict.
MIN_OBSERVATIONS_FOR_VERDICT = 20
BACKFILL_COVERAGE_FLOOR = 0.5
# A quiet stretch longer than this is investigated rather than assumed benign.
# NOT a reliable closure test on its own: the exchange was shut for 83 days
# across 1404-1405, and there are 74 market-wide closure days over 12 Jalali
# years. `marketdata.calendars.market_closure_days()` is the actual discriminator
# (market-wide zero volume AND zero trades).
MAX_OUTAGE_CALENDAR_DAYS = 21


def _as_gregorian_date(value, default: dt.date) -> dt.date:
    if value is None:
        return default
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    year, month, day = (int(part) for part in str(value).split("-")[:3])
    if year < 1700:
        return jdatetime.date(year, month, day).togregorian()
    return dt.date(year, month, day)


def _expected_sessions(
    start: dt.date, end: dt.date, *, tse_calendar: bool
) -> list[dt.date]:
    if start > end:
        raise ValueError("integrity window start must not be after end")
    sessions = []
    day = start
    while day <= end:
        if not tse_calendar or day.weekday() not in (3, 4):
            sessions.append(day)
        day += dt.timedelta(days=1)
    return sessions


def _jalali_text(day: dt.date) -> str:
    value = jdatetime.date.fromgregorian(date=day)
    return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"


def _stored_date(value: str) -> dt.date | None:
    try:
        year, month, day = (int(part) for part in value.split()[0].split("-"))
        return jdatetime.date(year, month, day).togregorian()
    except (TypeError, ValueError):
        return None


def compute_symbol_integrity(
    symbol: str,
    *,
    start=None,
    end=None,
    timeframe: str = "1d_adj",
    _tse_sessions: tuple[dt.date, ...] | None = None,
) -> dict[str, Any]:
    """Assess one symbol over an explicit Gregorian or Jalali date window."""
    instrument = MarketInstrument.objects.filter(symbol=symbol).first()
    if not instrument:
        return {
            "symbol": symbol,
            "passes_gate": False,
            "reason": "unknown_symbol",
            "reason_codes": ["unknown_symbol"],
        }

    end_date = _as_gregorian_date(end, timezone.now().date())
    start_date = _as_gregorian_date(start, end_date - dt.timedelta(days=179))
    tse_calendar = instrument.source == MarketInstrument.Source.TSETMC
    if tse_calendar:
        if _tse_sessions is None:
            market_days = actual_trading_days(
                start=_jalali_text(start_date), end=_jalali_text(end_date)
            )
            sessions = sorted(
                day for value in market_days
                if (day := _stored_date(value)) is not None
            )
        else:
            sessions = list(_tse_sessions)
        # A new/empty warehouse still needs a conservative integrity answer.
        if not sessions:
            sessions = _expected_sessions(start_date, end_date, tse_calendar=True)
    else:
        # Gold/FX symbols do not all quote seven days a week: USD/gold/USDT do,
        # EUR/GBP/CHF/CAD skip Fridays and holidays. Expecting every calendar
        # day failed the six-day symbols at 0.833 coverage and dropped them from
        # the universe. Use the days the feed itself broadly published.
        quoting = gold_currency_quoting_days(
            start=_jalali_text(start_date), end=_jalali_text(end_date)
        )
        sessions = sorted(
            day for value in quoting
            if (day := _stored_date(value)) is not None
            and start_date <= day <= end_date
        )
        if not sessions:
            sessions = _expected_sessions(start_date, end_date, tse_calendar=False)
    expected = set(sessions)

    if instrument.source == MarketInstrument.Source.TSETMC:
        raw_dates = (
            candle_close_qs(symbol)
            if timeframe == MarketCandle.ADJUSTED
            else MarketCandle.objects.filter(symbol=symbol, timeframe=timeframe)
        ).values_list("date_time", flat=True)
    else:
        raw_dates = GoldCurrencyHistory.objects.filter(symbol=symbol).values_list(
            "date", flat=True
        )

    observed = {
        day for value in raw_dates
        if (day := _stored_date(value)) is not None and day in expected
    }

    # A symbol listed (or first backfilled) partway into the window has no
    # history before its first print -- that is the absence of history, not a
    # hole in it. Scoring from the window start instead accused 537 of the 1,410
    # tracked symbols of low coverage over data that is completely clean, and
    # dropped every one of them from the returns matrix. `_build_returns_matrix`
    # already reasons this way via `_gap_profile`; the two layers must agree or
    # the gate silently overrules the matrix.
    first_observed = min(observed) if observed else None
    leading_gap = (
        len([day for day in sessions if day < first_observed])
        if first_observed is not None else len(sessions)
    )
    if first_observed is not None:
        sessions = [day for day in sessions if day >= first_observed]

    # Days this symbol was halted while the exchange traded. The provider prints
    # no candle for them and never will, so counting them as missing is a
    # permanent, unfixable failure for an asset whose data is fine.
    halted = set()
    if tse_calendar and sessions:
        halted = {
            day for value in symbol_halt_days(
                symbol, start=_jalali_text(sessions[0]), end=_jalali_text(end_date)
            )
            if (day := _stored_date(value)) is not None
        }
        sessions = [day for day in sessions if day not in halted]

    expected = set(sessions)
    observed &= expected
    observed_sessions = len(observed)
    expected_sessions = len(sessions)
    coverage = observed_sessions / expected_sessions if expected_sessions else 0.0
    last_valid = max(observed) if observed else None

    positions = {day: index for index, day in enumerate(sessions)}
    ordered = sorted(observed)
    max_gap = max(
        (positions[current] - positions[previous] - 1
         for previous, current in zip(ordered, ordered[1:])),
        default=0,
    )
    freshness = (
        len([day for day in sessions if last_valid is None or day > last_valid])
        if sessions else 0
    )
    if tse_calendar:
        labels = (
            ("stock_candle_adjusted", "series:1d_adj")
            if timeframe == MarketCandle.ADJUSTED
            else ("stock_candle_unadjusted", "series:1d_unadj")
        )
    else:
        labels = ("gold_daily", "crypto_daily")
    rejected_count = (
        RejectedRecord.objects.filter(
            symbol=symbol,
            endpoint__in=labels,
            date__gte=_jalali_text(start_date),
            date__lte=_jalali_text(end_date),
        )
        .exclude(reason__startswith="field_")
        .count()
    )
    rejection_ratio = (
        rejected_count / (observed_sessions + rejected_count)
        if observed_sessions + rejected_count else 0.0
    )

    # Distinct from `low_coverage` on purpose. This says "the backfill has not
    # reached this symbol", which is a statement about our warehouse; the other
    # codes accuse the data of being broken. Conflating them is why the Ops
    # console reported 79% of the universe as an integrity failure when most of
    # it was simply un-fetched. Only asked of a window long enough to judge.
    not_backfilled = expected_sessions >= MIN_OBSERVATIONS_FOR_VERDICT and (
        observed_sessions < MIN_OBSERVATIONS_FOR_VERDICT
        or coverage < BACKFILL_COVERAGE_FLOOR
    )

    reason_codes = []
    if not_backfilled:
        reason_codes.append("insufficient_backfill")
    elif coverage < MIN_COVERAGE:
        reason_codes.append("low_coverage")
    if max_gap > MAX_FORWARD_FILL_SESSIONS:
        reason_codes.append("price_gap_exceeded")
    if freshness > MAX_FORWARD_FILL_SESSIONS:
        reason_codes.append("stale")
    if (
        rejected_count >= MIN_REJECTIONS_FOR_GATE
        and rejection_ratio > MAX_REJECTION_RATIO
    ):
        reason_codes.append("excessive_rejections")

    missing_sessions = [day for day in sessions if day not in observed]
    return {
        "symbol": symbol,
        "source": instrument.source,
        "timeframe": timeframe,
        "window_start": start_date.isoformat(),
        "window_end": end_date.isoformat(),
        # Where scoring actually began, once the pre-listing run and any halts
        # were removed. Differs from `window_start` for anything newly listed.
        "history_start": first_observed.isoformat() if first_observed else None,
        "leading_gap_sessions": leading_gap,
        "halted_sessions": len(halted),
        "observed_sessions": observed_sessions,
        "expected_sessions": expected_sessions,
        "coverage_ratio": coverage,
        "last_valid_date": last_valid.isoformat() if last_valid else None,
        "freshness_sessions": freshness,
        "max_gap_days": max_gap,
        "rejected_count": rejected_count,
        "rejection_ratio": rejection_ratio,
        "passes_gate": not reason_codes,
        "reason_codes": reason_codes,
        "reason": ",".join(reason_codes),
        # Non-fatal: a short but clean series is usable, the consumer just needs
        # to know it is short. Mirrors the matrix's `short_history` warning.
        "notes": (
            ["short_history"] if leading_gap > MAX_FORWARD_FILL_SESSIONS else []
        ),
        "missing_count": len(missing_sessions),
        "missing_dates": [_jalali_text(day) for day in missing_sessions[:20]],
    }


def market_outage_windows(start=None, end=None) -> list[tuple[dt.date, dt.date]]:
    """Stretches where the whole session calendar goes dark.

    Every other check here measures a symbol against `actual_trading_days()`,
    which is built from the candle table. That makes the per-symbol gate blind
    to a period where nothing was ingested at all: with no candles there are no
    sessions, so no symbol is missing any, and coverage reads as healthy. The
    only way to see it is to ask whether the calendar itself has a hole too
    long to be a holiday.
    """
    end_date = _as_gregorian_date(end, timezone.now().date())
    start_date = _as_gregorian_date(start, end_date - dt.timedelta(days=730))
    sessions = sorted(
        day
        for value in actual_trading_days(
            start=_jalali_text(start_date), end=_jalali_text(end_date)
        )
        if (day := _stored_date(value)) is not None
    )
    return [
        (previous, current)
        for previous, current in zip(sessions, sessions[1:])
        if (current - previous).days > MAX_OUTAGE_CALENDAR_DAYS
    ]


def update_all_symbols_integrity():
    """Persist current-window assessments for all eligible instruments."""
    end_date = timezone.now().date()
    start_date = end_date - dt.timedelta(days=179)
    instruments = list(
        MarketInstrument.objects.filter(eligible=True).values_list("symbol", "source")
    )
    tse_sessions = None
    if any(source == MarketInstrument.Source.TSETMC for _symbol, source in instruments):
        market_days = actual_trading_days(
            start=_jalali_text(start_date), end=_jalali_text(end_date)
        )
        tse_sessions = tuple(sorted(
            day for value in market_days
            if (day := _stored_date(value)) is not None
        ))
        if not tse_sessions:
            tse_sessions = tuple(
                _expected_sessions(start_date, end_date, tse_calendar=True)
            )

    results = []
    for symbol, source in instruments:
        metrics = compute_symbol_integrity(
            symbol,
            start=start_date,
            end=end_date,
            _tse_sessions=(
                tse_sessions
                if source == MarketInstrument.Source.TSETMC
                else None
            ),
        )
        obj, _ = SymbolIntegrity.objects.update_or_create(
            symbol=symbol,
            defaults={
                "source": metrics.get("source", ""),
                "coverage_ratio": metrics.get("coverage_ratio", 0.0),
                "max_gap_days": metrics.get("max_gap_days", 0),
                "rejected_count": metrics.get("rejected_count", 0),
                "passes_gate": metrics.get("passes_gate", False),
                "reason": metrics.get("reason", ""),
            },
        )
        results.append(obj)
    return results


def update_symbol_integrity(symbol: str) -> dict:
    """Recompute and persist the 179-day gate for one symbol."""
    metrics = compute_symbol_integrity(symbol)
    if metrics.get("reason_codes") == ["unknown_symbol"] or metrics.get("reason") == "unknown_symbol":
        return metrics
    SymbolIntegrity.objects.update_or_create(
        symbol=symbol,
        defaults={
            "source": metrics.get("source", ""),
            "coverage_ratio": metrics.get("coverage_ratio", 0.0),
            "max_gap_days": metrics.get("max_gap_days", 0),
            "rejected_count": metrics.get("rejected_count", 0),
            "passes_gate": metrics.get("passes_gate", False),
            "reason": metrics.get("reason", ""),
        },
    )
    return metrics

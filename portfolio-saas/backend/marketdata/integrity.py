"""Windowed integrity assessments for market-history series."""

import datetime as dt
from typing import Any

import jdatetime
from django.utils import timezone

from .candles import actual_trading_days, candle_close_qs
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

    reason_codes = []
    if coverage < MIN_COVERAGE:
        reason_codes.append("low_coverage")
    if max_gap > MAX_FORWARD_FILL_SESSIONS:
        reason_codes.append("price_gap_exceeded")
    if freshness > MAX_FORWARD_FILL_SESSIONS:
        reason_codes.append("stale")
    if rejection_ratio > MAX_REJECTION_RATIO:
        reason_codes.append("excessive_rejections")

    return {
        "symbol": symbol,
        "source": instrument.source,
        "timeframe": timeframe,
        "window_start": start_date.isoformat(),
        "window_end": end_date.isoformat(),
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
    }


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

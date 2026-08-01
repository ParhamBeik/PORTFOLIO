"""Windowed integrity assessments for market-history series."""

import datetime as dt
from typing import Any

import jdatetime
from django.utils import timezone

from .models import (
    GoldCurrencyHistory,
    MarketCandle,
    MarketInstrument,
    RejectedRecord,
    SymbolIntegrity,
)

MAX_FORWARD_FILL_SESSIONS = 5
MIN_COVERAGE = 0.90


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
    sessions = _expected_sessions(
        start_date,
        end_date,
        tse_calendar=instrument.source == MarketInstrument.Source.TSETMC,
    )
    expected = set(sessions)

    if instrument.source == MarketInstrument.Source.TSETMC:
        raw_dates = MarketCandle.objects.filter(
            symbol=symbol, timeframe=timeframe
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
    rejected_count = RejectedRecord.objects.filter(symbol=symbol).count()

    reason_codes = []
    if coverage < MIN_COVERAGE:
        reason_codes.append("low_coverage")
    if max_gap > MAX_FORWARD_FILL_SESSIONS:
        reason_codes.append("price_gap_exceeded")
    if freshness > MAX_FORWARD_FILL_SESSIONS:
        reason_codes.append("stale")
    if rejected_count:
        reason_codes.append("unresolved_rejections")

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
        "passes_gate": not reason_codes,
        "reason_codes": reason_codes,
        "reason": ",".join(reason_codes),
    }


def update_all_symbols_integrity():
    """Persist current-window assessments for all eligible instruments."""
    results = []
    for symbol in MarketInstrument.objects.filter(eligible=True).values_list(
        "symbol", flat=True
    ):
        metrics = compute_symbol_integrity(symbol)
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

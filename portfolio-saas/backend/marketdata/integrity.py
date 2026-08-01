"""Integrity gate checks for warehouse data."""

from typing import Dict, Any, List
import datetime
import jdatetime
from django.db import transaction
from django.utils import timezone
from .models import MarketInstrument, ArchiveFetchState, RejectedRecord, SymbolIntegrity, DailyStockHistory, MarketCandle, GoldCurrencyHistory

def compute_symbol_integrity(symbol: str) -> Dict[str, Any]:
    """Compute integrity metrics for a single symbol."""
    instrument = MarketInstrument.objects.filter(symbol=symbol).first()
    if not instrument:
        return {"passes_gate": False, "reason": "Unknown symbol"}
    
    # Determine type and source
    dates = []
    if instrument.source == "tse":
        candles = MarketCandle.objects.filter(symbol=symbol, timeframe="1d_adj").order_by("date_time")
        dates = [c.date_time.split()[0] for c in candles]
    else:
        gch = GoldCurrencyHistory.objects.filter(symbol=symbol).order_by("date")
        dates = [g.date for g in gch]

    history_count = len(dates)

    # Fetch archive expected rows
    archive_state = ArchiveFetchState.objects.filter(symbol=symbol).first()
    expected_rows = archive_state.expected_rows if archive_state and archive_state.expected_rows else history_count
    if expected_rows <= 0:
        expected_rows = 180

    coverage_ratio = history_count / expected_rows if expected_rows > 0 else 0.0
    
    # Calculate max gap days
    max_gap_days = 0
    if len(dates) > 1:
        prev_date = None
        for d_str in dates:
            try:
                parts = [int(p) for p in d_str.split("-")]
                curr_date = jdatetime.date(parts[0], parts[1], parts[2]).togregorian()
                if prev_date is not None:
                    # Calculate missing trading days (Tehran weekend is Thursday and Friday)
                    missing_trading_days = 0
                    temp_date = prev_date
                    while temp_date < curr_date:
                        temp_date = temp_date + datetime.timedelta(days=1)
                        if temp_date < curr_date:
                            # 3 is Thursday, 4 is Friday
                            if temp_date.weekday() not in (3, 4):
                                missing_trading_days += 1
                    if missing_trading_days > max_gap_days:
                        max_gap_days = missing_trading_days
                prev_date = curr_date
            except Exception:
                continue

    # Rejections
    rejected_count = RejectedRecord.objects.filter(symbol=symbol).count()
    
    passes_gate = True
    reason = ""
    
    if coverage_ratio < 0.95:
        passes_gate = False
        reason = f"Low coverage ratio ({coverage_ratio:.2%})"
    elif max_gap_days > 10:
        passes_gate = False
        reason = f"Max gap days exceeded ({max_gap_days} days)"
    elif rejected_count > 0:
        passes_gate = False
        reason = f"Unresolved rejections exist ({rejected_count})"
        
    return {
        "symbol": symbol,
        "source": instrument.source,
        "coverage_ratio": coverage_ratio,
        "max_gap_days": max_gap_days,
        "rejected_count": rejected_count,
        "passes_gate": passes_gate,
        "reason": reason,
    }

def update_all_symbols_integrity():
    """Update integrity checks for all eligible symbols."""
    symbols = MarketInstrument.objects.filter(eligible=True).values_list('symbol', flat=True)
    results = []
    for symbol in symbols:
        metrics = compute_symbol_integrity(symbol)
        
        # update or create
        obj, created = SymbolIntegrity.objects.update_or_create(
            symbol=symbol,
            defaults={
                "source": metrics.get("source", ""),
                "coverage_ratio": metrics.get("coverage_ratio", 0.0),
                "max_gap_days": metrics.get("max_gap_days", 0),
                "rejected_count": metrics.get("rejected_count", 0),
                "passes_gate": metrics.get("passes_gate", False),
                "reason": metrics.get("reason", ""),
            }
        )
        results.append(obj)
    return results

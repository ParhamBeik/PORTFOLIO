"""Integrity gate checks for warehouse data."""

from typing import Dict, Any, List
from django.db import transaction
from django.utils import timezone
from .models import MarketInstrument, ArchiveFetchState, RejectedRecord, SymbolIntegrity, DailyStockHistory

def compute_symbol_integrity(symbol: str) -> Dict[str, Any]:
    """Compute integrity metrics for a single symbol."""
    instrument = MarketInstrument.objects.filter(symbol=symbol).first()
    if not instrument:
        return {"passes_gate": False, "reason": "Unknown symbol"}
    
    # Coverage ratio and gap days
    # (Assuming simple estimation for now; ideally compute from DailyStockHistory dates)
    history_count = DailyStockHistory.objects.filter(symbol=symbol).count()
    
    archive_state = ArchiveFetchState.objects.filter(symbol=symbol, endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED).first()
    
    expected_rows = archive_state.expected_rows if archive_state else history_count
    coverage_ratio = history_count / expected_rows if expected_rows and expected_rows > 0 else 0.0
    
    # Calculate max gap days here (Simplified)
    max_gap_days = 0 
    # Fetch dates and find gaps if history_count > 1...
    
    # Rejections
    rejected_count = RejectedRecord.objects.filter(symbol=symbol).count()
    
    passes_gate = True
    reason = ""
    
    if coverage_ratio < 0.95:
        passes_gate = False
        reason = "Low coverage ratio"
    elif max_gap_days > 10:
        passes_gate = False
        reason = "Max gap days exceeded"
    elif rejected_count > 0:
        passes_gate = False
        reason = "Unresolved rejections exist"
        
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

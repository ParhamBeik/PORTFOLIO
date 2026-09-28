"""A bounded, revision-aware census of what Explore can actually display."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import jalali
from .explore_api import _balance_sheets, _income_statements, _monthly_sales
from .models import MarketInstrument, ResearchCoverageSnapshot


FAMILIES = {
    "monthly_sales": _monthly_sales,
    "income": _income_statements,
    "balance_sheet": _balance_sheets,
}


def refresh_research_coverage(window_days=365):
    """Persist only a complete census; a failed scan leaves the last one intact."""
    if window_days not in {90, 365, 1825, 3650}:
        raise ValueError("window_days must be 90, 365, 1825, or 3650")
    started_at = timezone.now()
    end_date = timezone.localtime(started_at, jalali.TEHRAN).date()
    start = jalali.from_gregorian(end_date - timedelta(days=window_days))
    end = jalali.from_gregorian(end_date)
    universe = list(MarketInstrument.objects.filter(
        source=MarketInstrument.Source.TSETMC,
        category=MarketInstrument.Category.STOCK,
        eligible=True,
    ).order_by("symbol").values_list("symbol", flat=True))
    summary = {family: {
        "symbols_with_filing": 0,
        "symbols_with_verified": 0,
        "symbols_fully_verified": 0,
        "symbols_with_withheld": 0,
        "symbols_without_filing": 0,
        "verified_periods": 0,
        "withheld_periods": 0,
    } for family in FAMILIES}
    symbols = []
    for symbol in universe:
        item = {"symbol": symbol}
        for family, reader in FAMILIES.items():
            result = reader(symbol, start, end)
            filing = result["latest_filing_periods"]
            verified = result["verified_periods"]
            withheld = result["withheld_periods"]
            if filing != verified + withheld:
                raise ValueError(f"inconsistent {family} coverage for {symbol}")
            item[family] = {
                "status": result["status"],
                "latest_filing_periods": filing,
                "verified_periods": verified,
                "withheld_periods": withheld,
            }
            counts = summary[family]
            counts["symbols_with_filing"] += filing > 0
            counts["symbols_with_verified"] += verified > 0
            counts["symbols_fully_verified"] += filing > 0 and withheld == 0
            counts["symbols_with_withheld"] += withheld > 0
            counts["symbols_without_filing"] += filing == 0
            counts["verified_periods"] += verified
            counts["withheld_periods"] += withheld
        symbols.append(item)
    return ResearchCoverageSnapshot.objects.create(
        started_at=started_at,
        finished_at=timezone.now(),
        window_days=window_days,
        start_jalali=start,
        end_jalali=end,
        universe_size=len(universe),
        eligibility_version="explore_v1",
        parser_versions={
            "monthly_sales": settings.CODAL_PARSER_VERSION,
            "statements": settings.CODAL_STATEMENT_PARSER_VERSION,
        },
        summary=summary,
        symbols=symbols,
    )

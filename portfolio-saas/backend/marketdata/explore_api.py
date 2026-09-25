"""Read-only company exploration over source data, without inferred fundamentals.

The stock close is a provider observation, not an adjusted return or an
accounting fact. A second provider endpoint can disagree with it; report that
disagreement instead of quietly choosing a winner.
"""

from datetime import timedelta
from collections import defaultdict
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import jalali
from .models import (
    ArchiveFetchState,
    CodalAnnouncement,
    CodalCandidateFact,
    CodalReport,
    CodalVerification,
    DailyStockHistory,
    MarketCandle,
    MarketInstrument,
    StockSymbolMetadata,
)


def _instrument(symbol):
    return MarketInstrument.objects.filter(
        source=MarketInstrument.Source.TSETMC,
        category=MarketInstrument.Category.STOCK,
        eligible=True,
        symbol=symbol,
    ).first()


def _safe_codal_link(url):
    parsed = urlparse(url or "")
    return url if parsed.scheme == "https" and parsed.hostname in {"codal.ir", "www.codal.ir"} else None


def _monthly_sales(symbol, start, end):
    """Latest filing for each month wins; an unverified correction hides prior totals."""
    reports = CodalReport.objects.filter(
        announcement__symbol=symbol,
        category=CodalAnnouncement.Category.PRODUCTION_SALES,
        period_end__gte=start, period_end__lte=end,
    ).select_related("announcement").order_by(
        "-announcement__date_publish", "-announcement__time_publish", "-pk"
    )
    latest = {}
    for report in reports:
        latest.setdefault(report.period_end, report)
    candidates = defaultdict(list)
    for fact in CodalCandidateFact.objects.filter(
        extraction__report_id__in=[report.pk for report in latest.values()],
        extraction__parser_version=settings.CODAL_PARSER_VERSION,
        verification_status=CodalVerification.RECONCILED,
        fact_code="sales.revenue",
        unit="million_rial", currency="IRR",
    ).select_related("extraction__artifact"):
        candidates[fact.extraction.report_id].append(fact)

    points = []
    for period_end, report in sorted(latest.items()):
        eligible = [fact for fact in candidates[report.pk] if (
            fact.numeric_value is not None
            and fact.numeric_value >= 0
            and fact.period_end == period_end
            and fact.period_start == f"{period_end[:8]}01"
            and fact.dimensions.get("row_kind") == "total"
            and fact.extraction.checksum_sha256 == fact.extraction.artifact.checksum_sha256
        )]
        # Distinct reconciled readings of the same filing need a human review.
        if not eligible or len({fact.numeric_value for fact in eligible}) != 1:
            continue
        fact = max(eligible, key=lambda item: (item.extraction.parsed_at, item.pk))
        day = jalali.to_gregorian(period_end)
        if day is None:
            continue
        points.append({
            "date": day.isoformat(),
            "period_start_jalali": fact.period_start,
            "period_end_jalali": period_end,
            "value": str(fact.numeric_value),
            "source_url": _safe_codal_link(report.announcement.link),
            "published_jalali": report.announcement.date_publish,
            "is_correction": report.is_correction,
            "report_id": report.pk,
            "extraction_id": fact.extraction_id,
            "artifact_id": fact.extraction.artifact_id,
            "artifact_sha256": fact.extraction.checksum_sha256,
            "source_coordinates": fact.source_coordinates,
            "verification": CodalVerification.RECONCILED,
        })
    return {
        "status": (
            "verified" if points and len(points) == len(latest)
            else "partially_verified" if points else "unavailable_unverified"
        ),
        "measure": "monthly_sales_revenue",
        "unit": "million_rial",
        "currency": "IRR",
        "latest_filing_periods": len(latest),
        "verified_periods": len(points),
        "withheld_periods": len(latest) - len(points),
        "points": points,
    }


class StockSearchView(APIView):
    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if len(query) > 100:
            raise ValidationError({"q": "Search must be 100 characters or fewer."})
        rows = MarketInstrument.objects.filter(
            source=MarketInstrument.Source.TSETMC,
            category=MarketInstrument.Category.STOCK,
            eligible=True,
        )
        if query:
            rows = rows.filter(Q(symbol__icontains=query) | Q(name__icontains=query))
        return Response([
            {"symbol": row.symbol, "name": row.name, "isin": row.isin}
            for row in rows.order_by("symbol")[:30]
        ])


class StockDossierView(APIView):
    WINDOWS = {90, 365, 1825, 3650}

    def get(self, request, symbol):
        instrument = _instrument(symbol)
        if instrument is None:
            raise NotFound("No eligible TSE stock with this symbol was found.")
        try:
            days = int(request.query_params.get("days", "365"))
        except ValueError as exc:
            raise ValidationError({"days": "Choose 90, 365, 1825, or 3650 days."}) from exc
        if days not in self.WINDOWS:
            raise ValidationError({"days": "Choose 90, 365, 1825, or 3650 days."})

        end = timezone.localtime(timezone.now(), jalali.TEHRAN).date()
        start = jalali.from_gregorian(end - timedelta(days=days))
        end_jalali = jalali.from_gregorian(end)
        tomorrow_jalali = jalali.from_gregorian(end + timedelta(days=1))
        closes = list(DailyStockHistory.objects.filter(
            symbol=symbol, date__gte=start, date__lte=end_jalali, pl__gt=0,
        ).order_by("date").values_list("date", "pl"))
        points = [
            {"date": jalali.to_gregorian(day).isoformat(), "jalali_date": day, "close_rial": str(close)}
            for day, close in closes if jalali.to_gregorian(day) is not None
        ]

        # Match only the same unadjusted close on the same source-native day.
        # A mismatch is a visible quality flag; it does not rewrite either row.
        candles = {}
        for day, price in MarketCandle.objects.filter(
                symbol=symbol, timeframe=MarketCandle.UNADJUSTED,
                date_time__gte=start, date_time__lt=tomorrow_jalali,
            ).values_list("date_time", "close_price"):
            candles.setdefault(day.split(" ")[0], []).append(price)
        paired = [
            (day, close, [price for price in candles[day] if price > 0])
            for day, close in closes if day in candles and any(price > 0 for price in candles[day])
        ]
        mismatched = sum(
            1 for _, close, day_candles in paired
            if any(candle > 0 and abs(close - candle) / close > 0.01 for candle in day_candles)
        )

        metadata = StockSymbolMetadata.objects.filter(l18=symbol).order_by("-updated_at").first()
        states = {
            row.endpoint: row
            for row in ArchiveFetchState.objects.filter(
                symbol=symbol,
                endpoint__in=[
                    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
                    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
                    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
                ],
            )
        }
        disclosures = CodalAnnouncement.objects.filter(symbol=symbol).order_by(
            "-date_publish", "-time_publish"
        )[:8]
        return Response({
            "company": {
                "symbol": instrument.symbol,
                "name": metadata.l30 if metadata else instrument.name,
                "isin": metadata.isin if metadata else instrument.isin,
                "sector": metadata.sector if metadata else "",
                "subsector": metadata.sector_sub if metadata else "",
                "metadata_updated_at": metadata.updated_at.isoformat() if metadata else None,
            },
            "price": {
                "source": "TSETMC daily stock history via BrsApi",
                "series": "unadjusted_last_trade_close",
                "unit": "Rial per share",
                "days_requested": days,
                "points": points,
                "first_date": points[0]["jalali_date"] if points else None,
                "last_date": points[-1]["jalali_date"] if points else None,
                "paired_candle_days": len(paired),
                "candle_disagreements_over_1pct": mismatched,
                "quality": "cross_source_disagreement" if mismatched else "provider_reported_unreconciled",
            },
            "coverage": [
                {
                    "endpoint": endpoint,
                    "stored_rows": states[endpoint].stored_rows if endpoint in states else 0,
                    "missing_rows": states[endpoint].missing_rows if endpoint in states else None,
                    "verified_complete": states[endpoint].verified_complete if endpoint in states else False,
                    "last_success_at": (
                        states[endpoint].last_success_at.isoformat()
                        if endpoint in states and states[endpoint].last_success_at else None
                    ),
                }
                for endpoint in (
                    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
                    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
                    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
                )
            ],
            "disclosures": [
                {
                    "title": row.title,
                    "published_jalali": row.date_publish,
                    "category": row.category_title,
                    "source_url": _safe_codal_link(row.link),
                    "metric_status": "unverified",
                }
                for row in disclosures
            ],
            "financial_metrics": {
                "status": "unavailable_unverified",
                "reason": "Stored Codal extraction has not passed unit, period, and source reconciliation.",
            },
            "monthly_sales": _monthly_sales(symbol, start, end_jalali),
        })

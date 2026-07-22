"""Read-only endpoints over the market-data warehouse.

All views are DB-only range scans — no external API call ever happens in a
request handler (the sync/backfill tasks own fetching). Public market charts
(candles, history, index, symbols) are FREE; "smart-money" data (Codal filings,
shareholder moves) is a Pro differentiator alongside the analytics endpoints.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsPro
from portfolio.models import Asset

from .models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketIndexData,
    ShareholderRecord,
    StockSymbolMetadata,
)


def _limit(request, default, cap):
    try:
        value = int(request.query_params.get("limit", str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, 1), cap)


class CandlesView(APIView):
    """OHLCV series for one symbol. ?symbol=کاما&timeframe=1d_adj&limit=200"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        symbol = request.query_params.get("symbol")
        if not symbol:
            return Response({"detail": "symbol query param required."}, status=400)
        timeframe = request.query_params.get("timeframe", "1d_adj")
        rows = (
            MarketCandle.objects.filter(symbol=symbol, timeframe=timeframe)
            .order_by("-date_time")[: _limit(request, 200, 500)]
        )
        return Response([
            {
                "date_time": r.date_time,
                "open": float(r.open_price),
                "high": float(r.high_price),
                "low": float(r.low_price),
                "close": float(r.close_price),
                "volume": r.volume,
            }
            for r in reversed(list(rows))
        ])


class DailyHistoryView(APIView):
    """Daily close series for one symbol. ?symbol=کاما&adjusted=1&limit=365"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        symbol = request.query_params.get("symbol")
        if not symbol:
            return Response({"detail": "symbol query param required."}, status=400)
        adjusted = request.query_params.get("adjusted", "0") == "1"
        rows = (
            DailyStockHistory.objects.filter(symbol=symbol, is_adjusted=adjusted)
            .order_by("-date")[: _limit(request, 365, 730)]
        )
        return Response([
            {
                "date": r.date,
                "close": float(r.pl),
                "close_final": float(r.pc),
                "min": float(r.pmin),
                "max": float(r.pmax),
                "volume": r.tvol,
                "change_pct": r.plp,
            }
            for r in reversed(list(rows))
        ])


class MarketIndexView(APIView):
    """TSE overall + equal-weight index series. ?limit=365"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = MarketIndexData.objects.order_by("-date", "-time")[
            : _limit(request, 365, 730)
        ]
        return Response([
            {
                "date": r.date,
                "index_overall": r.index_overall,
                "index_overall_change": r.index_overall_change,
                "index_equal_weight": r.index_equal_weight,
                "trade_value": r.trade_value,
            }
            for r in reversed(list(rows))
        ])


class SymbolListView(APIView):
    """Synced TSE symbol metadata: name, sector, fundamentals."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = StockSymbolMetadata.objects.order_by("l18")
        return Response([
            {
                "symbol": r.l18,
                "name": r.l30,
                "sector": r.sector,
                "market": r.market,
                "eps": float(r.eps) if r.eps is not None else None,
                "pe": float(r.pe) if r.pe is not None else None,
                "market_cap": r.market_cap,
                "updated_at": r.updated_at.isoformat(),
            }
            for r in rows
        ])


class MarketAssetsView(APIView):
    """Public supported asset catalog with DB-backed archive coverage."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        assets = (
            Asset.objects.filter(is_active=True)
            .exclude(tse_symbol="", brs_symbol="")
            .order_by("asset_class", "name")
        )
        rows = []
        for asset in assets:
            if asset.tse_symbol:
                series = DailyStockHistory.objects.filter(
                    symbol=asset.tse_symbol,
                    is_adjusted=True,
                )
                source = "stock"
                symbol = asset.tse_symbol
            else:
                series = GoldCurrencyHistory.objects.filter(symbol=asset.brs_symbol)
                source = "gold"
                symbol = asset.brs_symbol
            dates = series.order_by("date").values_list("date", flat=True)
            first = dates.first()
            last = dates.last()
            rows.append({
                "key": asset.key,
                "name": asset.name,
                "symbol": symbol,
                "source": source,
                "records": series.count(),
                "first_date": first,
                "last_date": last,
            })
        return Response(rows)


class PerformanceView(APIView):
    """Unified full-history OHLC performance for one supported asset."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        asset_key = request.query_params.get("asset")
        asset = Asset.objects.filter(key=asset_key, is_active=True).first()
        if asset is None:
            return Response({"detail": "Unknown or unsupported asset."}, status=400)
        limit = _limit(request, 5000, 5000)
        if asset.tse_symbol:
            rows = list(
                DailyStockHistory.objects.filter(
                    symbol=asset.tse_symbol,
                    is_adjusted=True,
                ).order_by("-date")[:limit]
            )
            series = [
                {
                    "date": row.date,
                    "open": float(row.pf),
                    "high": float(row.pmax),
                    "low": float(row.pmin),
                    "close": float(row.pl),
                    "volume": row.tvol,
                }
                for row in reversed(rows)
            ]
            source = "stock"
            symbol = asset.tse_symbol
        elif asset.brs_symbol:
            rows = list(
                GoldCurrencyHistory.objects.filter(symbol=asset.brs_symbol)
                .order_by("-date")[:limit]
            )
            series = [
                {
                    "date": row.date,
                    "open": float(row.open_price) if row.open_price is not None else None,
                    "high": float(row.high_price) if row.high_price is not None else None,
                    "low": float(row.low_price) if row.low_price is not None else None,
                    "close": float(row.close_price),
                    "volume": None,
                }
                for row in reversed(rows)
            ]
            source = "gold"
            symbol = asset.brs_symbol
        else:
            return Response({"detail": "Asset has no verified provider source."}, status=400)

        return Response({
            "asset": {
                "key": asset.key,
                "name": asset.name,
                "symbol": symbol,
                "source": source,
            },
            "coverage": {
                "records": len(series),
                "first_date": series[0]["date"] if series else None,
                "last_date": series[-1]["date"] if series else None,
            },
            "series": series,
        })


class AnnouncementsView(APIView):
    """Pro: Codal disclosures, newest first. ?symbol=کاما&limit=20"""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        qs = CodalAnnouncement.objects.order_by("-date_publish", "-time_publish")
        symbol = request.query_params.get("symbol")
        if symbol:
            qs = qs.filter(symbol=symbol)
        rows = qs[: _limit(request, 20, 50)]
        return Response([
            {
                "symbol": r.symbol,
                "title": r.title,
                "date_publish": r.date_publish,
                "time_publish": r.time_publish,
                "link": r.link,
                "link_pdf": r.link_pdf,
            }
            for r in rows
        ])


class ShareholdersView(APIView):
    """Pro: latest institutional shareholder roster for one symbol."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        symbol = request.query_params.get("symbol")
        if not symbol:
            return Response({"detail": "symbol query param required."}, status=400)
        latest = (
            ShareholderRecord.objects.filter(symbol=symbol)
            .order_by("-date")
            .values_list("date", flat=True)
            .first()
        )
        rows = ShareholderRecord.objects.filter(symbol=symbol, date=latest or "").order_by(
            "-percent"
        )
        return Response([
            {
                "name": r.name,
                "percent": r.percent,
                "volume": r.volume,
                "change": float(r.change),
                "date": r.date,
            }
            for r in rows
        ])

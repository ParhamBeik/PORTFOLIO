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
        from .models import MarketInstrument

        # Standard active template assets first
        assets = (
            Asset.objects.filter(is_active=True)
            .exclude(tse_symbol="", brs_symbol="")
            .order_by("asset_class", "name")
        )
        seen_symbols = set()
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
            seen_symbols.add(symbol)
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

        # Include all eligible provider catalog instruments not already listed
        instruments = MarketInstrument.objects.filter(eligible=True).order_by("category", "symbol")
        for inst in instruments:
            if inst.symbol in seen_symbols:
                continue
            seen_symbols.add(inst.symbol)
            if inst.source == MarketInstrument.Source.TSETMC or inst.category == MarketInstrument.Category.STOCK:
                series = DailyStockHistory.objects.filter(symbol=inst.symbol)
                source = "stock"
            else:
                series = GoldCurrencyHistory.objects.filter(symbol=inst.symbol)
                source = "gold"
            dates = series.order_by("date").values_list("date", flat=True)
            first = dates.first()
            last = dates.last()
            rows.append({
                "key": f"sym:{inst.symbol}",
                "name": inst.name or inst.symbol,
                "symbol": inst.symbol,
                "source": source,
                "records": series.count(),
                "first_date": first,
                "last_date": last,
            })

        return Response(rows)


class PerformanceView(APIView):
    """Unified full-history OHLC performance for any supported asset or symbol."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import MarketInstrument

        asset_key = request.query_params.get("asset", "").strip()
        if not asset_key:
            return Response({"detail": "asset query param required."}, status=400)

        # 1. Check if asset_key matches an Asset.key
        asset = Asset.objects.filter(key=asset_key, is_active=True).first()
        limit = _limit(request, 5000, 5000)

        symbol = ""
        source = ""
        name = ""

        if asset:
            name = asset.name
            if asset.tse_symbol:
                symbol = asset.tse_symbol
                source = "stock"
            elif asset.brs_symbol:
                symbol = asset.brs_symbol
                source = "gold"
        else:
            # 2. Check if asset_key is a raw symbol or sym:<symbol>
            raw_sym = asset_key.replace("sym:", "").strip()
            inst = MarketInstrument.objects.filter(symbol=raw_sym).first()
            if inst:
                symbol = inst.symbol
                name = inst.name or inst.symbol
                source = "stock" if inst.source == MarketInstrument.Source.TSETMC else "gold"
            else:
                symbol = raw_sym
                name = raw_sym
                source = "stock"

        if source == "stock" or DailyStockHistory.objects.filter(symbol=symbol).exists():
            rows = list(
                DailyStockHistory.objects.filter(
                    symbol=symbol,
                ).order_by("-date")[:limit]
            )
            series = [
                {
                    "date": row.date,
                    "open": float(row.pf) if row.pf is not None else float(row.pl),
                    "high": float(row.pmax) if row.pmax is not None else float(row.pl),
                    "low": float(row.pmin) if row.pmin is not None else float(row.pl),
                    "close": float(row.pl),
                    "volume": row.tvol,
                }
                for row in reversed(rows)
            ]
            source = "stock"
        else:
            rows = list(
                GoldCurrencyHistory.objects.filter(symbol=symbol)
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

        return Response({
            "asset": {
                "key": asset_key,
                "name": name,
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


class QuotaStatusView(APIView):
    """GET /api/market/quota/ - Returns daily quota usage, 5-min window quota, and archive backfill progress."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import ArchiveFetchState
        from .quota import get_quota_status

        status_data = get_quota_status()
        total_states = ArchiveFetchState.objects.count()
        complete_states = ArchiveFetchState.objects.filter(verified_complete=True).count()
        progress_pct = round((complete_states / total_states * 100), 2) if total_states > 0 else 0.0
        status_data["archive_progress"] = {
            "total_states": total_states,
            "complete_states": complete_states,
            "pending_states": max(0, total_states - complete_states),
            "progress_pct": progress_pct,
        }
        return Response(status_data)


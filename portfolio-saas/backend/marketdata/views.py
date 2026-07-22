"""Read-only endpoints over the market-data warehouse.

All views are DB-only range scans — no external API call ever happens in a
request handler (the sync/backfill tasks own fetching). Public market charts
(candles, history, index, symbols) are FREE; "smart-money" data (Codal filings,
shareholder moves) is a Pro differentiator alongside the analytics endpoints.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import IsPro
from portfolio.models import Account, Asset, Holding, Price, Snapshot, Transaction

from .models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketIndexData,
    ShareholderRecord,
    StockSymbolMetadata,
    StockTransactionTick,
)


def _positive_stock_history(symbol):
    from django.db.models import Q
    qs = DailyStockHistory.objects.filter(symbol=symbol)
    if qs.filter(is_adjusted=True).filter(Q(pc__gt=0) | Q(pl__gt=0)).exists():
        return qs.filter(is_adjusted=True).filter(Q(pc__gt=0) | Q(pl__gt=0))
    return qs.filter(Q(pc__gt=0) | Q(pl__gt=0))


def _positive_gold_history(symbol):
    return GoldCurrencyHistory.objects.filter(symbol=symbol, close_price__gt=0)


def _asset_source(asset):
    if asset.asset_class == Asset.AssetClass.STOCK:
        return "stock"
    if asset.asset_class == Asset.AssetClass.CASH:
        return "currency"
    return "gold"


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
        adjusted = request.query_params.get("adjusted", "1") == "1"
        rows = (
            DailyStockHistory.objects.filter(symbol=symbol, is_adjusted=adjusted, pl__gt=0)
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


class TicksView(APIView):
    """Intraday transaction ticks for one symbol. ?symbol=کاما&date=1403-10-19&limit=500"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        symbol = request.query_params.get("symbol")
        if not symbol:
            return Response({"detail": "symbol query param required."}, status=400)
        qs = StockTransactionTick.objects.filter(symbol=symbol)
        date = request.query_params.get("date")
        if date:
            qs = qs.filter(date=date)
        rows = qs.order_by("-date", "-row")[: _limit(request, 500, 1000)]
        return Response([
            {
                "date": r.date,
                "time": r.time,
                "row": r.row,
                "price": float(r.price),
                "volume": r.volume,
                "canceled": r.canceled,
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
        from django.db.models import Count, Max, Min, Q
        from .models import MarketInstrument

        # Bulk fetch metadata, stock stats, and gold stats in 3 fast queries
        all_metadata = {row.l18: row for row in StockSymbolMetadata.objects.all()}

        stock_stats = {
            r["symbol"]: r
            for r in DailyStockHistory.objects.filter(Q(pc__gt=0) | Q(pl__gt=0))
            .values("symbol")
            .annotate(first_date=Min("date"), last_date=Max("date"), records=Count("id"))
        }

        gold_stats = {
            r["symbol"]: r
            for r in GoldCurrencyHistory.objects.filter(close_price__gt=0)
            .values("symbol")
            .annotate(first_date=Min("date"), last_date=Max("date"), records=Count("id"))
        }

        assets = (
            Asset.objects.filter(is_active=True)
            .exclude(tse_symbol="", brs_symbol="")
            .order_by("asset_class", "name")
        )

        seen_symbols = set()
        rows = []
        for asset in assets:
            if asset.tse_symbol:
                symbol = asset.tse_symbol
                source = "stock"
                stats = stock_stats.get(symbol, {})
            else:
                symbol = asset.brs_symbol
                source = _asset_source(asset)
                stats = gold_stats.get(symbol, {})

            meta = all_metadata.get(symbol)
            seen_symbols.add(symbol)
            rows.append({
                "key": asset.key,
                "name": asset.name,
                "name_fa": asset.name_fa,
                "asset_class": asset.asset_class,
                "currency": asset.currency,
                "symbol": symbol,
                "source": source,
                "sector": meta.sector if meta else "",
                "sector_sub": meta.sector_sub if meta else "",
                "pe": float(meta.pe) if meta and meta.pe is not None else None,
                "eps": float(meta.eps) if meta and meta.eps is not None else None,
                "market_cap": meta.market_cap if meta else None,
                "records": stats.get("records", 0),
                "first_date": stats.get("first_date"),
                "last_date": stats.get("last_date"),
            })

        instruments = MarketInstrument.objects.filter(eligible=True).order_by("category", "symbol")
        for inst in instruments:
            if inst.symbol in seen_symbols:
                continue
            seen_symbols.add(inst.symbol)
            meta = all_metadata.get(inst.symbol)
            if inst.source == MarketInstrument.Source.TSETMC or inst.category == MarketInstrument.Category.STOCK:
                source = "stock"
                asset_class = Asset.AssetClass.STOCK
                sector = inst.provider_group or (meta.sector if meta else "")
                sector_sub = meta.sector_sub if meta else ""
                stats = stock_stats.get(inst.symbol, {})
            else:
                source = "currency" if inst.provider_group == "currency" or inst.symbol in ("USD", "USDT_IRT") else "gold"
                asset_class = Asset.AssetClass.CASH if source == "currency" else Asset.AssetClass.GOLD
                sector = inst.provider_group
                sector_sub = ""
                stats = gold_stats.get(inst.symbol, {})

            rows.append({
                "key": f"sym:{inst.symbol}",
                "name": inst.name or inst.symbol,
                "name_fa": "",
                "asset_class": asset_class,
                "currency": "USD" if inst.symbol == "USD" else "IRT",
                "symbol": inst.symbol,
                "source": source,
                "sector": sector,
                "sector_sub": sector_sub,
                "pe": float(meta.pe) if meta and meta.pe is not None else None,
                "eps": float(meta.eps) if meta and meta.eps is not None else None,
                "market_cap": meta.market_cap if meta else None,
                "records": stats.get("records", 0),
                "first_date": stats.get("first_date"),
                "last_date": stats.get("last_date"),
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
        if not asset and Asset.objects.filter(key=asset_key).exists():
            return Response({"detail": "asset is inactive."}, status=400)
        limit = _limit(request, 5000, 5000)

        symbol = ""
        source = ""
        name = ""

        if asset:
            name = asset.name
            if asset.tse_symbol:
                symbol = asset.tse_symbol
            elif asset.brs_symbol:
                symbol = asset.brs_symbol
            source = _asset_source(asset)
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

        if source == "stock" or _positive_stock_history(symbol).exists():
            rows = list(
                _positive_stock_history(symbol).order_by("-date")[:limit]
            )
            series = []
            for row in reversed(rows):
                c_price = float(row.pc) if (row.pc and row.pc > 0) else float(row.pl)
                series.append({
                    "date": row.date,
                    "open": float(row.pf) if (row.pf and row.pf > 0) else c_price,
                    "high": float(row.pmax) if (row.pmax and row.pmax > 0) else c_price,
                    "low": float(row.pmin) if (row.pmin and row.pmin > 0) else c_price,
                    "close": c_price,
                    "volume": row.tvol,
                })
            source = "stock"
        else:
            rows = list(
                _positive_gold_history(symbol).order_by("-date")[:limit]
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
            if source != "currency":
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
                "category": r.category,
                "category_title": r.category_title,
                "is_audited": r.is_audited,
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


class AdminStatusView(APIView):
    """Staff-only operational snapshot for data coverage, users, and storage."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_staff:
            return Response({"detail": "Admin access required."}, status=403)

        from .models import ApiRequestQuota, ArchiveFetchState, MarketInstrument
        from .quota import get_quota_status

        total_states = ArchiveFetchState.objects.count()
        complete_states = ArchiveFetchState.objects.filter(verified_complete=True).count()
        pending_states = max(0, total_states - complete_states)
        failures = ArchiveFetchState.objects.filter(consecutive_failures__gt=0).count()
        latest_quota = ApiRequestQuota.objects.order_by("-day").first()
        users = User.objects.order_by("-date_joined")[:20]
        recent_snapshots = Snapshot.objects.order_by("-timestamp")[:10]

        return Response({
            "users": {
                "total": User.objects.count(),
                "staff": User.objects.filter(is_staff=True).count(),
                "pro": sum(1 for user in User.objects.all() if user.is_pro()),
                "recent": [
                    {
                        "id": user.id,
                        "email": user.email,
                        "tier": user.tier,
                        "is_staff": user.is_staff,
                        "joined": user.date_joined.isoformat(),
                    }
                    for user in users
                ],
            },
            "database": {
                "accounts": Account.objects.count(),
                "holdings": Holding.objects.count(),
                "prices": Price.objects.count(),
                "snapshots": Snapshot.objects.count(),
                "transactions": Transaction.objects.count(),
                "market_instruments": MarketInstrument.objects.count(),
                "stock_history_rows": DailyStockHistory.objects.count(),
                "gold_currency_rows": GoldCurrencyHistory.objects.count(),
                "candles": MarketCandle.objects.count(),
                "announcements": CodalAnnouncement.objects.count(),
                "shareholders": ShareholderRecord.objects.count(),
            },
            "archive": {
                "total_states": total_states,
                "complete_states": complete_states,
                "pending_states": pending_states,
                "failed_states": failures,
                "progress_pct": round((complete_states / total_states * 100), 2) if total_states else 0,
                "worst_gaps": [
                    {
                        "endpoint": row.endpoint,
                        "symbol": row.symbol,
                        "stored_rows": row.stored_rows,
                        "expected_rows": row.expected_rows,
                        "missing_rows": row.missing_rows,
                        "last_error": row.last_error,
                        "consecutive_failures": row.consecutive_failures,
                    }
                    for row in ArchiveFetchState.objects.order_by("-missing_rows", "-consecutive_failures")[:100]
                ],
            },
            "quota": get_quota_status(),
            "latest_quota_day": str(latest_quota.day) if latest_quota else None,
            "recent_snapshots": [
                {
                    "id": row.id,
                    "user_id": row.user_id,
                    "account_id": row.account_id,
                    "total": str(row.total_value_tomans),
                    "timestamp": row.timestamp.isoformat(),
                }
                for row in recent_snapshots
            ],
        })

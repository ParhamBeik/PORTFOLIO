"""Read-only endpoints over the market-data warehouse.

All views are DB-only range scans — no external API call ever happens in a
request handler (the sync/backfill tasks own fetching). Public market charts
(candles, history, index, symbols) are FREE; "smart-money" data (Codal filings,
shareholder moves) is a Pro differentiator alongside the analytics endpoints.
"""
from django.views import View
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import IsPro
from portfolio.models import Account, Asset, Holding, Price, Snapshot, Transaction

from .candles import candle_close_qs
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
        queryset = (
            candle_close_qs(symbol)
            if timeframe == MarketCandle.ADJUSTED
            else MarketCandle.objects.filter(symbol=symbol, timeframe=timeframe)
        )
        rows = queryset.order_by("-date_time")[: _limit(request, 200, 500)]
        return Response([
            {
                "date_time": r.date_time,
                "open": float(r.open_price),
                "high": float(r.high_price),
                "low": float(r.low_price),
                "close": float(r.close_price),
                "volume": r.volume,
                "source": getattr(r, "candle_source", r.timeframe),
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
        if adjusted:
            rows = (
                candle_close_qs(symbol)
                .order_by("-date_time")[: _limit(request, 365, 730)]
            )
            return Response([
                {
                    "date": r.date_time,
                    "close": float(r.close_price),
                    "close_final": float(r.close_price),
                    "min": float(r.low_price),
                    "max": float(r.high_price),
                    "volume": r.volume,
                    "change_pct": 0.0,
                    "source": r.candle_source,
                }
                for r in reversed(list(rows))
            ])
        else:
            rows = (
                DailyStockHistory.objects.filter(symbol=symbol, is_adjusted=False, pl__gt=0)
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
        import numpy as np
        from portfolio.services.returns import daily_returns_matrix

        # Bulk fetch metadata, stock stats, and gold stats in 3 fast queries
        all_metadata = {row.l18: row for row in StockSymbolMetadata.objects.all()}
        stock_symbols = set(
            MarketInstrument.objects.filter(
                source=MarketInstrument.Source.TSETMC
            ).values_list("symbol", flat=True)
        )
        stock_symbols.update(
            Asset.objects.filter(is_active=True)
            .exclude(tse_symbol="")
            .values_list("tse_symbol", flat=True)
        )

        stock_stats = {
            r["symbol"]: r
            for r in candle_close_qs(stock_symbols)
            .values("symbol")
            .annotate(first_date=Min("date_time"), last_date=Max("date_time"), records=Count("id"))
        }

        gold_stats = {
            r["symbol"]: r
            for r in GoldCurrencyHistory.objects.filter(close_price__gt=0)
            .values("symbol")
            .annotate(first_date=Min("date"), last_date=Max("date"), records=Count("id"))
        }

        # Compute 1-year performance metrics (returns, volatility, Sharpe) for active assets
        perf_metrics = {}
        try:
            df, _ = daily_returns_matrix(history_days=365)
            for col in df.columns:
                series = df[col].dropna()
                if not series.empty:
                    daily_vol = series.std()
                    ann_vol = daily_vol * np.sqrt(240)
                    total_ret = (1 + series).prod() - 1
                    n_days = len(series)
                    years = n_days / 240.0
                    ann_ret = (total_ret + 1) ** (1.0 / years) - 1 if years > 0 else 0.0
                    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
                    
                    perf_metrics[col] = {
                        "return_1y": float(total_ret),
                        "volatility_1y": float(ann_vol),
                        "sharpe_1y": float(sharpe),
                    }
        except Exception:
            # Safeguard so if pandas returns matrix fails, market assets list still loads
            pass

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
            
            m = perf_metrics.get(asset.key) if asset.key in perf_metrics else None
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
                "return_1y": m["return_1y"] if m else None,
                "volatility_1y": m["volatility_1y"] if m else None,
                "sharpe_1y": m["sharpe_1y"] if m else None,
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
                "return_1y": None,
                "volatility_1y": None,
                "sharpe_1y": None,
            })

        return Response(rows)


class CompareView(APIView):
    """Ranked, filterable precomputed market metrics."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        from .models import AssetMetricSnapshot

        allowed = {
            "total_return",
            "annualized_volatility",
            "sharpe",
            "sortino",
            "max_drawdown",
            "beta",
            "correlation",
        }
        sort = request.query_params.get("sort", "sharpe")
        if sort not in allowed:
            return Response(
                {"detail": f"sort must be one of {sorted(allowed)}."},
                status=400,
            )
        try:
            window = int(request.query_params.get("window", "365"))
        except ValueError:
            return Response({"detail": "window must be an integer."}, status=400)
        queryset = AssetMetricSnapshot.objects.filter(window_days=window)
        asset_class = request.query_params.get("asset_class")
        if asset_class:
            queryset = queryset.filter(asset_class=asset_class)
        latest = queryset.order_by("-as_of").values_list("as_of", flat=True).first()
        if latest:
            queryset = queryset.filter(as_of=latest)
        direction = "" if request.query_params.get("order") == "asc" else "-"
        rows = queryset.order_by(f"{direction}{sort}", "symbol")[
            : _limit(request, 100, 500)
        ]
        return Response([
            {
                "symbol": row.symbol,
                "asset_class": row.asset_class,
                "as_of": row.as_of,
                "window_days": row.window_days,
                "total_return": row.total_return,
                "annualized_volatility": row.annualized_volatility,
                "sharpe": row.sharpe,
                "sortino": row.sortino,
                "max_drawdown": row.max_drawdown,
                "beta": row.beta,
                "correlation": row.correlation,
            }
            for row in rows
        ])


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

        if source == "stock" or candle_close_qs(symbol).exists():
            rows = list(
                candle_close_qs(symbol).order_by("-date_time")[:limit]
            )
            series = []
            for row in reversed(rows):
                series.append({
                    "date": row.date_time,
                    "open": float(row.open_price),
                    "high": float(row.high_price),
                    "low": float(row.low_price),
                    "close": float(row.close_price),
                    "volume": row.volume,
                    "source": row.candle_source,
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


class AdminStatusView(APIView):
    """Staff-only operational snapshot for data coverage, users, and storage."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        from .models import (
            ApiRequestQuota,
            ArchiveFetchState,
            MarketInstrument,
            StockTransactionTick,
        )
        from .logs import get_recent_logs
        from .quota import get_quota_status

        total_states = ArchiveFetchState.objects.count()
        complete_states = ArchiveFetchState.objects.filter(verified_complete=True).count()
        pending_states = max(0, total_states - complete_states)
        failures = ArchiveFetchState.objects.filter(consecutive_failures__gt=0).count()
        latest_quota = ApiRequestQuota.objects.order_by("-day").first()
        users = User.objects.order_by("-date_joined")[:20]
        recent_snapshots = Snapshot.objects.order_by("-timestamp")[:10]

        category_summary = {}
        for ep_choice, ep_label in ArchiveFetchState.Endpoint.choices:
            states = ArchiveFetchState.objects.filter(endpoint=ep_choice)
            t_cnt = states.count()
            c_cnt = states.filter(verified_complete=True).count()
            f_cnt = states.filter(consecutive_failures__gt=0).count()
            category_summary[ep_choice] = {
                "label": ep_label,
                "total_states": t_cnt,
                "complete_states": c_cnt,
                "pending_states": max(0, t_cnt - c_cnt),
                "failed_states": f_cnt,
                "progress_pct": round((c_cnt / t_cnt * 100), 2) if t_cnt else 0,
            }

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
                "portfolio_transactions": Transaction.objects.count(),
                "stock_transaction_ticks": StockTransactionTick.objects.count(),
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
                "category_summary": category_summary,
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
                    for row in ArchiveFetchState.objects.order_by("verified_complete", "-missing_rows", "-consecutive_failures")[:100]
                ],
            },
            "quota": get_quota_status(),
            "latest_quota_day": str(latest_quota.day) if latest_quota else None,
            "recent_logs": get_recent_logs(100),
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


def _get_cached_db_counts():
    from django.core.cache import cache
    cached = cache.get("db_counts_diagnostics")
    if cached:
        return cached

    from django.db import connection
    from portfolio.models import Account, Holding, Price, Snapshot, Transaction
    from .models import StockTransactionTick, MarketInstrument, DailyStockHistory, GoldCurrencyHistory, MarketCandle, CodalAnnouncement, ShareholderRecord

    models_map = {
        "accounts": Account,
        "holdings": Holding,
        "prices": Price,
        "snapshots": Snapshot,
        "transactions": Transaction,
        "portfolio_transactions": Transaction,
        "stock_transaction_ticks": StockTransactionTick,
        "market_instruments": MarketInstrument,
        "stock_history_rows": DailyStockHistory,
        "gold_currency_rows": GoldCurrencyHistory,
        "candles": MarketCandle,
        "announcements": CodalAnnouncement,
        "shareholders": ShareholderRecord,
    }

    counts = {}
    with connection.cursor() as cursor:
        for key, model in models_map.items():
            table_name = model._meta.db_table
            cursor.execute("SELECT reltuples FROM pg_class WHERE relname = %s", [table_name])
            row = cursor.fetchone()
            if row is not None and row[0] >= 0:
                counts[key] = int(row[0])
            else:
                counts[key] = model.objects.count()

    cache.set("db_counts_diagnostics", counts, 10)
    return counts


class AdminStatusStreamView(View):
    """Staff-only real-time SSE stream of system diagnostics, rate limits, and live logs."""

    async def get(self, request):
        import asyncio
        import json
        from asgiref.sync import sync_to_async
        from django.db import connections
        from django.http import StreamingHttpResponse, HttpResponseForbidden
        from rest_framework_simplejwt.authentication import JWTAuthentication
        from .logs import get_recent_logs
        from .models import ApiRequestQuota, ArchiveFetchState, MarketInstrument, StockTransactionTick
        from .quota import get_quota_status

        def get_authenticated_user():
            user = request.user
            if not user or not user.is_authenticated:
                token_str = request.GET.get("token")
                if token_str:
                    try:
                        auth_layer = JWTAuthentication()
                        validated_token = auth_layer.get_validated_token(token_str)
                        return auth_layer.get_user(validated_token)
                    except Exception:
                        return None
            return user

        user = await sync_to_async(get_authenticated_user)()

        if not user or not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return HttpResponseForbidden("Admin access required for SSE system stream.")

        async def stream_generator():
            iterations = 0
            while iterations < 300:
                iterations += 1

                def get_data():
                    # Release/cleanup any leaked connection before queries, then perform queries.
                    connections.close_all()

                    total_states = ArchiveFetchState.objects.count()
                    complete_states = ArchiveFetchState.objects.filter(verified_complete=True).count()
                    pending_states = max(0, total_states - complete_states)
                    failures = ArchiveFetchState.objects.filter(consecutive_failures__gt=0).count()
                    latest_quota = ApiRequestQuota.objects.order_by("-day").first()

                    category_summary = {}
                    for ep_choice, ep_label in ArchiveFetchState.Endpoint.choices:
                        states = ArchiveFetchState.objects.filter(endpoint=ep_choice)
                        t_cnt = states.count()
                        c_cnt = states.filter(verified_complete=True).count()
                        f_cnt = states.filter(consecutive_failures__gt=0).count()
                        category_summary[ep_choice] = {
                            "label": ep_label,
                            "total_states": t_cnt,
                            "complete_states": c_cnt,
                            "pending_states": max(0, t_cnt - c_cnt),
                            "failed_states": f_cnt,
                            "progress_pct": round((c_cnt / t_cnt * 100), 2) if t_cnt else 0,
                        }

                    worst_gaps = [
                        {
                            "endpoint": row.endpoint,
                            "symbol": row.symbol,
                            "stored_rows": row.stored_rows,
                            "expected_rows": row.expected_rows,
                            "missing_rows": row.missing_rows,
                            "last_error": row.last_error,
                            "consecutive_failures": row.consecutive_failures,
                        }
                        for row in ArchiveFetchState.objects.order_by("verified_complete", "-missing_rows", "-consecutive_failures")[:100]
                    ]

                    db_counts = _get_cached_db_counts()
                    quota_status = get_quota_status()
                    recent_logs = get_recent_logs(100)

                    # Release database connection immediately after queries to free pg client slots
                    connections.close_all()

                    return (
                        total_states,
                        complete_states,
                        pending_states,
                        failures,
                        latest_quota,
                        category_summary,
                        worst_gaps,
                        db_counts,
                        quota_status,
                        recent_logs,
                    )

                (
                    total_states,
                    complete_states,
                    pending_states,
                    failures,
                    latest_quota,
                    category_summary,
                    worst_gaps,
                    db_counts,
                    quota_status,
                    recent_logs,
                ) = await sync_to_async(get_data)()

                payload = {
                    "timestamp": asyncio.get_running_loop().time(),
                    "database": db_counts,
                    "archive": {
                        "total_states": total_states,
                        "complete_states": complete_states,
                        "pending_states": pending_states,
                        "failed_states": failures,
                        "progress_pct": round((complete_states / total_states * 100), 2) if total_states else 0,
                        "category_summary": category_summary,
                        "worst_gaps": worst_gaps,
                    },
                    "quota": quota_status,
                    "latest_quota_day": str(latest_quota.day) if latest_quota else None,
                    "recent_logs": recent_logs,
                }

                yield f"data: {json.dumps(payload)}\n\n"
                await asyncio.sleep(2.0)

        response = StreamingHttpResponse(stream_generator(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

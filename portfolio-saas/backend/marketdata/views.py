"""Read-only endpoints over the market-data warehouse.

All views are DB-only range scans — no external API call ever happens in a
request handler (the sync/backfill tasks own fetching). Public market charts
(candles, history, index, symbols) are FREE; "smart-money" data (Codal filings,
shareholder moves) is a Pro differentiator alongside the analytics endpoints.
"""
from django.views import View
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Exists, OuterRef, Q
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import RequiresFeature
from portfolio.models import Account, Asset, Holding, Price, Snapshot, Transaction

from .candles import candle_close_qs
from .models import (
    CodalAnnouncement,
    CodalFact,
    CodalReport,
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
                "open": float(r.open_price) if r.open_price is not None else None,
                "high": float(r.high_price) if r.high_price is not None else None,
                "low": float(r.low_price) if r.low_price is not None else None,
                "close": float(r.close_price),
                "volume": r.volume,
                "source": getattr(r, "candle_source", r.timeframe),
                "unit": "IRR",
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
                    "min": float(r.low_price) if r.low_price is not None else None,
                    "max": float(r.high_price) if r.high_price is not None else None,
                    "volume": r.volume,
                    "change_pct": 0.0,
                    "source": r.candle_source,
                    "unit": "IRR",
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
                    "min": float(r.pmin) if r.pmin is not None else None,
                    "max": float(r.pmax) if r.pmax is not None else None,
                    "volume": r.tvol,
                    "change_pct": r.plp,
                    "unit": "IRR",
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
                "unit": "IRR",
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
                "trade_value_unit": "IRR",
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
        from django.core.cache import cache
        from django.db.models import Count, Max, Min
        from .models import MarketCandle, MarketInstrument
        import numpy as np
        from portfolio.services.returns import daily_returns_matrix

        cache_key = "market:assets:catalog:v2"
        cached_rows = cache.get(cache_key)
        if cached_rows is not None:
            return Response(cached_rows)

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

        # Coverage stats only — avoid candle_close_qs Exists() which scans millions
        # of rows. Index (timeframe, symbol, date_time) keeps this aggregation fast.
        stock_stats = {
            r["symbol"]: r
            for r in MarketCandle.objects.filter(
                symbol__in=stock_symbols,
                timeframe__in=(MarketCandle.ADJUSTED, MarketCandle.AGGREGATE),
                close_price__gt=0,
            )
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

        cache.set(cache_key, rows, timeout=300)
        return Response(rows)


class CompareView(APIView):
    """Ranked, filterable precomputed market metrics."""

    permission_classes = [IsAuthenticated, RequiresFeature("market_compare")]

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
                    "open": float(row.open_price) if row.open_price is not None else None,
                    "high": float(row.high_price) if row.high_price is not None else None,
                    "low": float(row.low_price) if row.low_price is not None else None,
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
                # series[].close is Rial on the TSE branch and Toman (or the
                # provider's own unit for XAUUSD/BTC) on the gold branch. Same
                # key, different scales, so the unit has to travel with it --
                # a client that assumes one of them is 10x wrong on the other.
                "unit": "IRR" if source == "stock" else self._gold_unit(symbol),
            },
            "coverage": {
                "records": len(series),
                "first_date": series[0]["date"] if series else None,
                "last_date": series[-1]["date"] if series else None,
            },
            "series": series,
        })

    @staticmethod
    def _gold_unit(symbol):
        """The provider's declared unit for a BRS symbol, defaulting to Toman."""
        from .models import GoldCurrencyHistory

        return (
            GoldCurrencyHistory.objects.filter(symbol=symbol)
            .values_list("unit", flat=True)
            .first()
        ) or "تومان"


class AnnouncementsView(APIView):
    """Pro: Codal disclosures, newest first. ?symbol=کاما&limit=20"""

    permission_classes = [IsAuthenticated, RequiresFeature("market_announcements")]

    def get(self, request):
        qs = CodalAnnouncement.objects.select_related("report").order_by("-date_publish", "-time_publish")
        symbol = request.query_params.get("symbol")
        if symbol:
            qs = qs.filter(symbol=symbol)
        rows = qs[: _limit(request, 20, 50)]
        response = []
        for r in rows:
            report = getattr(r, "report", None)
            response.append({
                "symbol": r.symbol,
                "title": r.title,
                "category": r.category,
                "category_title": r.category_title,
                "is_audited": r.is_audited,
                "date_publish": r.date_publish,
                "time_publish": r.time_publish,
                "link": r.link,
                "link_pdf": r.link_pdf,
                "report_id": report.pk if report else None,
                "derived_category": report.category if report else r.category,
                "derived_type": report.report_type if report else r.category_title,
                "period": report.period_end if report else "",
                "is_consolidated": report.is_consolidated if report else False,
                "is_correction": report.is_correction if report else False,
                "revision_of": report.revision_of_id if report else None,
                "extraction_status": report.status if report else CodalReport.Status.PENDING,
                "facts_available": bool(report and report.facts.exists()),
            })
        return Response(response)


def _fact_payload(fact):
    return {
        "id": fact.pk,
        "report_id": fact.report_id,
        "symbol": fact.report.announcement.symbol,
        "category": fact.report.category,
        "fact_code": fact.fact_code,
        "numeric_value": fact.numeric_value,
        "text_value": fact.text_value,
        "unit": fact.unit,
        "currency": fact.currency,
        "period_start": fact.period_start,
        "period_end": fact.period_end,
        "dimensions": fact.dimensions,
        "confidence": fact.confidence,
        "quality": fact.quality,
        "parser_version": fact.parser_version,
        "source_coordinates": fact.source_coordinates,
    }


class ReportDetailView(APIView):
    permission_classes = [IsAuthenticated, RequiresFeature("market_announcements")]

    def get(self, request, report_id):
        report = get_object_or_404(
            CodalReport.objects.select_related("announcement", "revision_of"), pk=report_id
        )
        from .codal_pipeline import presigned_artifact_url

        artifacts = []
        for artifact in report.artifacts.all():
            try:
                download_url = presigned_artifact_url(artifact)
            except Exception:
                download_url = None
            artifacts.append({
                "id": artifact.pk,
                "kind": artifact.kind,
                "source_url": artifact.source_url,
                "checksum_sha256": artifact.checksum_sha256,
                "content_type": artifact.content_type,
                "size_bytes": artifact.size_bytes,
                "fetch_status": artifact.fetch_status,
                "error_code": artifact.error_code,
                "download_url": download_url,
            })
        revisions = CodalReport.objects.filter(
            announcement__symbol=report.announcement.symbol,
            report_type=report.report_type,
            period_end=report.period_end,
        ).order_by("announcement__date_publish", "announcement__time_publish")
        return Response({
            "id": report.pk,
            "announcement_id": report.announcement_id,
            "symbol": report.announcement.symbol,
            "title": report.announcement.title,
            "category": report.category,
            "report_type": report.report_type,
            "letter_type": report.letter_type,
            "period_start": report.period_start,
            "period_end": report.period_end,
            "is_audited": report.is_audited,
            "is_consolidated": report.is_consolidated,
            "is_correction": report.is_correction,
            "revision_of": report.revision_of_id,
            "parser_version": report.parser_version,
            "status": report.status,
            "quality": report.quality,
            "artifacts": artifacts,
            "tables": list(report.parsed_tables.values(
                "id", "name", "sheet_name", "headers", "rows", "source_coordinates", "parser_version"
            )),
            "sections": list(report.sections.values(
                "id", "heading", "body", "source_coordinates", "confidence"
            )),
            "revision_history": list(revisions.values(
                "id", "announcement__date_publish", "status", "quality", "parser_version"
            )),
        })


class ReportFactsView(APIView):
    permission_classes = [IsAuthenticated, RequiresFeature("market_announcements")]

    def get(self, request, report_id):
        report = get_object_or_404(CodalReport, pk=report_id)
        facts = report.facts.select_related("report__announcement").order_by("fact_code", "id")
        return Response([_fact_payload(fact) for fact in facts])


class FactsView(APIView):
    permission_classes = [IsAuthenticated, RequiresFeature("market_announcements")]

    def get(self, request):
        newer_revision = CodalReport.objects.filter(
            revision_of=OuterRef("report_id"), status=CodalReport.Status.PARSED
        )
        facts = (
            CodalFact.objects.select_related("report__announcement")
            .annotate(has_newer_revision=Exists(newer_revision))
            .filter(has_newer_revision=False, report__status=CodalReport.Status.PARSED)
        )
        filters = {
            "report__announcement__symbol": request.query_params.get("symbol"),
            "report__category": request.query_params.get("category"),
            "fact_code": request.query_params.get("fact_code"),
            "period_end": request.query_params.get("period"),
            "quality": request.query_params.get("quality"),
        }
        facts = facts.filter(**{key: value for key, value in filters.items() if value})
        facts = facts.order_by("-period_end", "fact_code")[: _limit(request, 100, 1000)]
        return Response([_fact_payload(fact) for fact in facts])


class ShareholdersView(APIView):
    """Pro: latest institutional shareholder roster for one symbol."""

    permission_classes = [IsAuthenticated, RequiresFeature("market_shareholders")]

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

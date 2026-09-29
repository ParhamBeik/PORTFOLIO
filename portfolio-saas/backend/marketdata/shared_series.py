"""Read-only market observations for the separate News Intelligence application."""

from __future__ import annotations

import hmac
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import jalali
from .models import MarketDailyBar, MarketIndexData
from .provenance import rejected_pairs


class SharedMarketSeriesView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        expected = settings.NEWS_MARKET_SERVICE_KEY
        supplied = request.headers.get("X-News-Service-Key", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return Response({"detail": "Service access denied"}, status=403)
        key = request.query_params.get("key", "")
        if key not in {"tehran_index", "bitcoin", "oil"}:
            return Response({"detail": "Unknown public instrument"}, status=404)
        try:
            days = int(request.query_params.get("days", "30"))
        except ValueError:
            return Response({"detail": "days must be an integer"}, status=400)
        if not 1 <= days <= 365:
            return Response({"detail": "days must be 1 to 365"}, status=400)
        since = jalali.from_gregorian(timezone.now() - timedelta(days=days))
        if key == "tehran_index":
            rows = MarketIndexData.objects.filter(date__gte=since).order_by("date", "time")
            daily = {}
            for row in rows.iterator():
                if row.index_overall > 0:
                    daily[row.date] = row
            points = [
                {"observed_at": jalali.to_datetime(row.date, row.time), "price": row.index_overall,
                 "quality": "provider_observation", "provider": "TSETMC",
                 "time_precision": "minute" if row.time else "date"}
                for row in daily.values()
            ]
            return Response({"key": key, "unit": "index points", "provider": "TSETMC",
                             "resolution": "daily last observation", "points": points,
                             "caveats": [] if points else ["no_verified_series"]})

        asset_class = "crypto" if key == "bitcoin" else "commodity"
        symbol = "BTC" if key == "bitcoin" else (
            MarketDailyBar.objects.filter(asset_class="commodity", symbol__icontains="BRENT")
            .values_list("symbol", flat=True).first()
        )
        if not symbol:
            return Response({"key": key, "unit": None, "provider": None,
                             "resolution": "daily", "points": [], "caveats": ["no_verified_series"]})
        rejected = rejected_pairs([symbol], since=since)
        bars = MarketDailyBar.objects.filter(
            asset_class=asset_class, symbol=symbol, date__gte=since,
            close_price__isnull=False,
        ).order_by("date")
        points = [
            # A daily close belongs at the END of its Jalali market day. Anchoring it
            # at midnight at the start would put tomorrow's closing price before news.
            {"observed_at": jalali.to_datetime(row.date) + timedelta(days=1),
             "price": row.close_price, "quality": "validated_daily_close",
             "provider": "BRS/Wallex market-data warehouse", "time_precision": "date"}
            for row in bars if (symbol, row.date) not in rejected and row.close_price > 0
        ]
        return Response({"key": key, "symbol": symbol, "unit": "USDT" if key == "bitcoin" else "provider quote",
                         "provider": "market-data warehouse", "resolution": "daily close", "points": points,
                         "caveats": [] if points else ["no_verified_series"]})

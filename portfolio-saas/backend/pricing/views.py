"""Pricing endpoints: latest prices, history, and Pro insights."""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsPro
from portfolios.models import Price
from portfolios.services import get_latest_prices
from .insights import build_insights


class LatestPricesView(APIView):
    """The shared global price map everyone reads. Cached and global."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        prices = get_latest_prices()
        return Response({
            k: float(v) for k, v in prices.items()
        })


class PriceHistoryView(APIView):
    """Time-series for one asset, for charts. ?asset=kama_stock&limit=100."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        asset_key = request.query_params.get("asset")
        if not asset_key:
            return Response({"detail": "asset query param required."}, status=400)
        # Cap the window (H6): an unbounded ?limit= could pull the whole series.
        limit = min(max(int(request.query_params.get("limit", "100")), 1), 500)
        rows = (
            Price.objects.filter(asset__key=asset_key)
            .order_by("-fetched_at")[:limit]
        )
        return Response([
            {"price": float(r.price), "fetched_at": r.fetched_at.isoformat()}
            for r in rows
        ])


class InsightsView(APIView):
    """Pro-tier financial insights. Free users get a 403 here."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        return Response(build_insights(request.user))

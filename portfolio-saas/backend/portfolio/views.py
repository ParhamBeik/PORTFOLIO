"""All portfolio endpoints: CRUD, live valuation, prices, and Pro analytics.

Valuation is computed live on read (holdings x latest prices) and the heavy
part (latest prices) is cached, so these endpoints stay cheap at scale.
Pro endpoints (insights/analytics/optimization) are gated by IsPro.
"""
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsPro

from .models import Account, Asset, Holding, Price, Snapshot
from .serializers import AccountSerializer, AssetSerializer, HoldingSerializer
from .services import get_latest_prices, value_account, value_user
from .services.diagnostics import portfolio_diagnostics
from .services.insights import _liquid_items, _total, build_insights
from .services.optimization import (
    SCENARIOS,
    UniverseTooSmall,
    _efficient_frontier,
    _finite,
    optimize,
)
from .services.returns import correlation_matrix, daily_returns_matrix


class AssetListView(generics.ListAPIView):
    """The investable asset catalog. Public to any authenticated user."""

    queryset = Asset.objects.filter(is_active=True)
    serializer_class = AssetSerializer


class AccountListCreateView(generics.ListCreateAPIView):
    serializer_class = AccountSerializer

    def get_queryset(self):
        return self.request.user.accounts.all()

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class AccountDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = AccountSerializer

    def get_queryset(self):
        return self.request.user.accounts.all()


class HoldingListCreateView(generics.ListCreateAPIView):
    serializer_class = HoldingSerializer

    def get_queryset(self):
        account = self._account()
        return account.holdings.all() if account else Holding.objects.none()

    def _account(self):
        return (
            self.request.user.accounts.filter(pk=self.kwargs["account_id"]).first()
        )

    def perform_create(self, serializer):
        account = self._account()
        if account is None:
            from rest_framework.exceptions import NotFound
            raise NotFound("Account not found")
        serializer.save(account=account)


class HoldingDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = HoldingSerializer

    def get_queryset(self):
        return Holding.objects.filter(account__user=self.request.user)


class ValuationView(APIView):
    """Current valuation for the whole user (all accounts)."""

    def get(self, request):
        return Response(_with_usd(value_user(request.user)))


class AccountValuationView(APIView):
    """Current valuation for one account."""

    def get(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        result = value_account(account)
        return Response({
            "id": account.id,
            "name": account.name,
            **_with_usd(result),
        })


def _with_usd(valuation: dict) -> dict:
    """Attach a USD equivalent of the total using the USD price in Tomans.

    Omitted entirely when there is no USD rate (M3): a real 0 would be
    indistinguishable from 'we know the rate and it is zero'.
    """
    prices = valuation.get("prices", {})
    usd_rate = Decimal(prices.get("usd_cash", 0) or 0)
    if usd_rate:
        valuation["total_usd"] = valuation["total"] / usd_rate
    return valuation


class SnapshotListView(APIView):
    """Per-user net-worth history for the FREE trend chart.

    The cron stamps one `account=None` row per user per fetch (the whole-portfolio
    total); this endpoint returns that series, oldest-first, capped at `days`.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            days = int(request.query_params.get("days", "30"))
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 365))
        since = timezone.now() - timedelta(days=days)
        rows = (
            Snapshot.objects.filter(user=request.user, account=None, timestamp__gte=since)
            .order_by("timestamp")
            .values("timestamp", "total_value_tomans")
        )
        return Response(
            [
                {"timestamp": r["timestamp"].isoformat(), "total": str(r["total_value_tomans"])}
                for r in rows
            ]
        )


class LatestPricesView(APIView):
    """The shared global price map everyone reads. Cached and global."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        prices = get_latest_prices()
        return Response({k: float(v) for k, v in prices.items()})


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


def _current_weights_and_total(user) -> tuple[dict[str, float], Decimal]:
    """Liquid weights + liquid total for a user (real estate excluded)."""
    valuation = value_user(user)
    items = _liquid_items(valuation)
    total = _total(items)
    if total <= 0:
        return {}, Decimal("0")
    weights = {
        i["key"]: float(i["value"] / total)
        for i in items
        if i["value"] > 0
    }
    return weights, total


class AnalyticsView(APIView):
    """Pro-tier portfolio diagnostics: vol, Sharpe, drawdown, VaR, etc."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        weights, total = _current_weights_and_total(request.user)
        return Response(
            portfolio_diagnostics(weights, total, user=request.user)
        )


class OptimizationView(APIView):
    """Pro-tier scenario optimizer: max_sharpe / min_volatility / risk_parity / hrp."""

    permission_classes = [IsAuthenticated, IsPro]

    def post(self, request):
        scenario = request.data.get("scenario")
        if scenario not in SCENARIOS:
            return Response(
                {"detail": f"scenario must be one of {list(SCENARIOS)}."},
                status=400,
            )
        constraints = request.data.get("constraints")
        weights, total = _current_weights_and_total(request.user)
        try:
            payload = optimize(
                scenario=scenario,
                current_weights=weights,
                total_value_tomans=total,
                constraints=constraints,
                user=request.user,
            )
        except UniverseTooSmall as exc:
            return Response(
                {
                    "detail": "Not enough price history yet to optimize this portfolio.",
                    "eligible_assets": exc.eligible,
                },
                status=503,
            )
        return Response(payload)


class FrontierView(APIView):
    """Pro-tier efficient frontier + max_sharpe / min_volatility reference points."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        weights, total = _current_weights_and_total(request.user)
        frontier = _efficient_frontier(n_points=30)
        # Inject the current portfolio point.
        returns, _ = daily_returns_matrix()
        current_point = None
        if weights and not returns.empty:
            cols = [k for k in weights if k in returns.columns]
            if cols:
                w = np.array([weights[k] for k in cols], dtype=float)
                if w.sum() > 0:
                    w = w / w.sum()
                    sub = returns[cols].fillna(0.0).to_numpy()
                    port = pd.Series(sub @ w, index=returns.index)
                    if port.std(ddof=1) > 0:
                        ann_ret = float(port.mean() * 252)
                        ann_vol = float(port.std(ddof=1) * np.sqrt(252))
                        current_point = {
                            "return": _finite(ann_ret),
                            "volatility": _finite(ann_vol),
                            "weights": weights,
                        }
        return Response({
            "frontier": frontier["frontier"],
            "max_sharpe": frontier["max_sharpe"],
            "min_volatility": frontier["min_volatility"],
            "current": current_point,
        })


class AssetReturnsView(APIView):
    """Pro-tier daily-returns matrix + correlation, for heatmaps and scatter plots."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        try:
            days = int(request.query_params.get("days", "180"))
        except ValueError:
            days = 180
        days = max(1, min(days, 365))
        df, excluded = daily_returns_matrix(history_days=days)
        if df.empty:
            return Response({
                "assets": [],
                "dates": [],
                "returns": {},
                "correlation": {"assets": [], "matrix": []},
                "excluded_assets": excluded,
            })
        assets = list(df.columns)
        dates = [d.isoformat() for d in df.index]
        returns_payload = {
            k: [None if np.isnan(v) else float(v) for v in df[k].tolist()]
            for k in assets
        }
        return Response({
            "assets": assets,
            "dates": dates,
            "returns": returns_payload,
            "correlation": correlation_matrix(),
            "excluded_assets": excluded,
        })

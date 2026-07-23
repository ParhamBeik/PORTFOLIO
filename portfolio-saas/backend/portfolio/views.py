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

from .models import Account, Asset, Holding, Price, Snapshot, Transaction
from .serializers import (
    AccountSerializer,
    AssetSerializer,
    HoldingSerializer,
    TradeInputSerializer,
    TransactionSerializer,
)
from .services import execute_trade, get_latest_prices, undo_trade, value_account, value_user
from .services.trades import TradeError
from .services.diagnostics import portfolio_diagnostics
from .services.insights import _liquid_items, _total, build_insights
from .services.optimization import (
    SCENARIOS,
    UniverseTooSmall,
    SolverError,
    NoAssetBeatsRiskFreeRate,
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
        if not serializer.validated_data["asset"].is_house:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Tradeable assets must be changed through the buy/sell endpoint.")
        serializer.save(account=account)


class HoldingDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = HoldingSerializer

    def get_queryset(self):
        return Holding.objects.filter(account__user=self.request.user)

    def perform_update(self, serializer):
        if not serializer.instance.asset.is_house:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Tradeable assets must be changed through the buy/sell endpoint.")
        serializer.save()

    def perform_destroy(self, instance):
        if not instance.asset.is_house:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Tradeable assets must be changed through the buy/sell endpoint.")
        instance.delete()


class TradeView(APIView):
    """Execute a buy/sell in one account (the ledger write path).

    POST /accounts/<id>/trades/  {asset_key, side, quantity, note?}
    Atomically appends a Transaction, updates the Holding balance, and snapshots
    net worth so the history chart steps at the trade moment.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            return Response({"detail": "Account not found."}, status=status.HTTP_404_NOT_FOUND)
        form = TradeInputSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        asset = Asset.objects.filter(key=data["asset_key"], is_active=True).first()
        if asset is None:
            return Response({"detail": "Unknown asset_key."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = execute_trade(
                account=account,
                asset=asset,
                side=data["side"],
                quantity=data["quantity"],
                note=data.get("note", ""),
            )
        except TradeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_201_CREATED)


class TransactionListView(APIView):
    """Trade history for the user (all accounts), newest first, capped by ?days=."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            days = int(request.query_params.get("days", "90"))
        except (TypeError, ValueError):
            days = 90
        days = max(1, min(days, 3650))
        since = timezone.now() - timedelta(days=days)
        rows = Transaction.objects.filter(
            account__user=request.user, timestamp__gte=since
        ).select_related("asset")
        account_id = request.query_params.get("account")
        if account_id:
            # Non-numeric ?account= would raise ValueError -> 500; ignore it.
            try:
                rows = rows.filter(account_id=int(account_id))
            except (TypeError, ValueError):
                return Response({"detail": "account must be an integer id."}, status=400)
        return Response(TransactionSerializer(rows, many=True).data)


class ValuationView(APIView):
    """Current valuation, optionally scoped to one portfolio via `?account=`.

    No `?account=` (or "All portfolios") aggregates every account the user owns.
    """

    def get(self, request):
        account = _scope(request)
        if account is not None:
            valuation = value_account(account)
            # value_account omits the price map; attach it so _with_usd can
            # convert to USD like the aggregate path does.
            valuation["prices"] = get_latest_prices()
        else:
            valuation = value_user(request.user)
        return Response(_with_usd(valuation))


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


def _scope(request):
    """Resolve the active portfolio from `?account=<id>`, owned by the user.

    Returns the Account or None. None means "all portfolios" (the aggregate
    view). An absent or non-numeric param, or an id the user does not own,
    all collapse to None rather than 400 — the client falls back to aggregate.
    """
    raw = request.query_params.get("account")
    if not raw:
        return None
    try:
        return request.user.accounts.filter(pk=int(raw)).first()
    except (TypeError, ValueError):
        return None


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
    """Per-user net-worth history for the FREE trend chart, plus trade markers.

    The cron stamps one `account=None` row per user per fetch (the whole-portfolio
    total), and every trade stamps one too; this endpoint returns that series
    oldest-first, capped at `days`. `trades` carries the buy/sell events in the
    same window so the chart can annotate the exact points where holdings changed.

    `?account=<id>` scopes both the snapshot series and the trade markers to one
    portfolio (reads that account's per-account snapshot rows); absent = aggregate.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            days = int(request.query_params.get("days", "30"))
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 365))
        since = timezone.now() - timedelta(days=days)
        account = _scope(request)
        snapshots = Snapshot.objects.filter(
            user=request.user, timestamp__gte=since, total_value_tomans__gt=0
        )
        if account is not None:
            snapshots = snapshots.filter(account=account)
        else:
            snapshots = snapshots.filter(account=None)
        prices = get_latest_prices()
        usd_rate = Decimal(prices.get("usd_cash", 0) or 0)
        rows = list(snapshots.order_by("timestamp").values("timestamp", "total_value_tomans"))
        raw_values = [Decimal(r["total_value_tomans"]) for r in rows if Decimal(r["total_value_tomans"]) > 0]
        
        # Sanity filtering for corrupted zero/extreme glitch rows
        median_val = sorted(raw_values)[len(raw_values) // 2] if raw_values else Decimal("0")
        
        # Group snapshots by calendar date: YYYY-MM-DD -> latest valid snapshot entry
        snapshot_by_day = {}
        for r in rows:
            val_toman = Decimal(r["total_value_tomans"])
            if val_toman <= 0:
                continue
            if len(raw_values) > 10 and median_val > 0:
                if val_toman > median_val * Decimal("50.0") or val_toman < median_val * Decimal("0.01"):
                    continue
            val_usd = str(round(val_toman / usd_rate, 2)) if usd_rate > 0 else None
            d_str = r["timestamp"].strftime("%Y-%m-%d")
            snapshot_by_day[d_str] = {
                "timestamp": r["timestamp"].isoformat(),
                "date": d_str,
                "total": str(r["total_value_tomans"]),
                "total_usd": val_usd,
            }

        # Dynamic daily history covering full `days` window (7, 30, 90, 365)
        from portfolio.services.valuation import compute_dynamic_net_worth_series
        base_series = compute_dynamic_net_worth_series(request.user, account, days=days)

        # Merge snapshot rows into daily base series so missing days are backfilled
        series = []
        for point in base_series:
            d_key = point.get("date")
            if d_key and d_key in snapshot_by_day:
                series.append(snapshot_by_day[d_key])
            else:
                series.append(point)
        trades = (
            Transaction.objects.filter(account__user=request.user, timestamp__gte=since)
            .select_related("asset")
            .order_by("timestamp")
        )
        if account is not None:
            trades = trades.filter(account=account)
        markers = [
            {
                "timestamp": t.timestamp.isoformat(),
                "side": t.side,
                "asset_key": t.asset.key,
                "asset_name": t.asset.name,
                "quantity": str(t.quantity),
                "price_tomans": str(t.price_tomans),
            }
            for t in trades
        ]
        return Response({"series": series, "trades": markers})


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
        return Response(build_insights(request.user, _scope(request)))


def _current_weights_and_total(user, account=None) -> tuple[dict[str, float], Decimal]:
    """Liquid weights + liquid total (real estate excluded).

    `account=None` analyzes the whole-user portfolio; passing an account scopes
    weights to that single portfolio.
    """
    valuation = value_account(account) if account is not None else value_user(user)
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
        weights, total = _current_weights_and_total(request.user, _scope(request))
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
        weights, total = _current_weights_and_total(request.user, _scope(request))
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
        except NoAssetBeatsRiskFreeRate as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except SolverError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(payload)


class FrontierView(APIView):
    """Pro-tier efficient frontier + max_sharpe / min_volatility reference points."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        weights, total = _current_weights_and_total(request.user, _scope(request))
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


class TransactionDestroyView(APIView):
    """Undo the latest trade for an asset and reverse its holding effect."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            undo_trade(user=request.user, transaction_id=pk)
        except Transaction.DoesNotExist:
            return Response({"detail": "Transaction not found."}, status=status.HTTP_404_NOT_FOUND)
        except TradeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "Transaction undone successfully."})

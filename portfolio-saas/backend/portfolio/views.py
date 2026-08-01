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
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsPro

from .models import Account, Asset, Holding, LedgerEntry, Price, Snapshot, Transaction
from .serializers import (
    AccountSerializer,
    AssetSerializer,
    HoldingSerializer,
    LedgerEntryInputSerializer,
    LedgerEntrySerializer,
    TradeInputSerializer,
    TransactionSerializer,
)
from .services import execute_trade, get_latest_prices, undo_trade, value_account, value_user
from .services.trades import TradeError
from .services.ledger import LedgerError, create_ledger_entry, reverse_ledger_entry
from .services.imports import (
    LedgerImportError,
    commit_ledger_import,
    preview_ledger_import,
)
from .services.performance import account_performance
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
from .services.returns import daily_returns_matrix


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
            raise NotFound("Account not found")
        if not serializer.validated_data["asset"].is_house:
            raise ValidationError("Tradeable assets must be changed through the buy/sell endpoint.")
        if account.holdings.filter(asset=serializer.validated_data["asset"]).exists():
            raise ValidationError("This asset already exists in the account.")
        serializer.save(account=account)


class HoldingDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = HoldingSerializer

    def get_queryset(self):
        return Holding.objects.filter(
            account__user=self.request.user,
            account_id=self.kwargs["account_id"],
        )

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


class LedgerListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def _account(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            raise NotFound("Account not found.")
        return account

    def get(self, request, account_id):
        account = self._account(request, account_id)
        rows = LedgerEntry.objects.filter(account=account).select_related("asset")
        return Response(LedgerEntrySerializer(rows, many=True).data)

    def post(self, request, account_id):
        account = self._account(request, account_id)
        form = LedgerEntryInputSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        asset = None
        if data.get("asset_key"):
            asset = Asset.objects.filter(key=data["asset_key"], is_active=True).first()
            if asset is None:
                return Response({"detail": "Unknown asset_key."}, status=400)
        try:
            entry = create_ledger_entry(
                account=account,
                asset=asset,
                kind=data["kind"],
                quantity=data.get("quantity"),
                unit_price_tomans=data.get("unit_price_tomans"),
                amount_tomans=data.get("amount_tomans"),
                occurred_at=data["occurred_at"],
                source=data["source"],
                note=data.get("note", ""),
                external_id=data.get("external_id", ""),
            )
        except LedgerError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(LedgerEntrySerializer(entry).data, status=201)


class LedgerReverseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, account_id, entry_id):
        try:
            reversal = reverse_ledger_entry(
                user=request.user, account_id=account_id, entry_id=entry_id
            )
        except LedgerEntry.DoesNotExist:
            return Response({"detail": "Ledger entry not found."}, status=404)
        except LedgerError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(LedgerEntrySerializer(reversal).data, status=201)


class LedgerImportView(APIView):
    permission_classes = [IsAuthenticated]
    commit = False

    def post(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            return Response({"detail": "Account not found."}, status=404)
        try:
            if not self.commit:
                return Response(preview_ledger_import(account, request.FILES.get("file")))
            batch, created = commit_ledger_import(account, request.FILES.get("file"))
        except LedgerImportError as exc:
            payload = {"detail": exc.detail}
            if exc.row is not None:
                payload["row"] = exc.row
            return Response(payload, status=400)
        return Response(
            {"batch_id": batch.id, "row_count": batch.row_count},
            status=201 if created else 200,
        )


class LedgerImportCommitView(LedgerImportView):
    commit = True


class AccountPerformanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            return Response({"detail": "Account not found."}, status=404)
        try:
            payload = account_performance(
                account, basis=request.query_params.get("basis")
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(payload)


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
                price_tomans=data.get("price_tomans"),
                timestamp=data["timestamp"],
                source=data["source"],
                note=data.get("note", ""),
            )
        except TradeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_201_CREATED)


class TransactionUndoView(APIView):
    """Roll back a specific transaction by ID."""

    permission_classes = [IsAuthenticated]

    def post(self, request, tx_id):
        try:
            undo_trade(user=request.user, transaction_id=tx_id)
        except Transaction.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        except TradeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "Transaction undone successfully."}, status=status.HTTP_200_OK)



import logging

from rest_framework.permissions import IsAdminUser
from portfolio.management.commands.clean_mispriced_data import audit_and_repair_prices

admin_logger = logging.getLogger("portfolio.admin")


class AdminCleanPricesScanView(APIView):
    """Scan database for mispriced price rows and corrupted snapshots (Admin only)."""

    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        stats = audit_and_repair_prices(fix=False)
        return Response(stats)


class AdminCleanPricesExecuteView(APIView):
    """Execute database cleanup: delete corrupted price rows, repair snapshots, and log action (Admin only).

    Destructive and irreversible (permanently deletes Price/Snapshot rows), so
    it requires the caller to echo back CONFIRM_PHRASE rather than firing on a
    bare POST — a single accidental click must not be enough to trigger it.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]
    CONFIRM_PHRASE = "DELETE MISPRICED DATA"

    def post(self, request):
        if request.data.get("confirm") != self.CONFIRM_PHRASE:
            return Response(
                {"detail": f'This is destructive and irreversible. Send {{"confirm": "{self.CONFIRM_PHRASE}"}} to execute.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        stats = audit_and_repair_prices(fix=True)
        admin_logger.info("[ADMIN_ACTION] %s executed price cleanup: %s", request.user.email, stats)
        return Response(stats, status=status.HTTP_200_OK)


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
        as_of = request.query_params.get("as_of")
        basis = request.query_params.get("basis") or "nominal"
        account = _scope(request)

        # Historical as-of (any basis) must use the warehouse path.
        # Live requests keep the live price path; usd_real is applied after.
        if as_of:
            from portfolio.services.valuation import value_as_of
            res = value_as_of(request.user, account=account, as_of=as_of, basis=basis)
            return Response(res)

        if account is not None:
            valuation = value_account(account)
            # value_account omits the price map; attach it so _with_usd can
            # convert to USD like the aggregate path does.
            valuation["prices"] = get_latest_prices()
        else:
            valuation = value_user(request.user)
        valuation = _with_usd(valuation)
        if basis == "usd_real":
            valuation = _express_usd_real(valuation)
        return Response(valuation)


class AccountValuationView(APIView):
    """Current valuation for one account."""

    def get(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        as_of = request.query_params.get("as_of")
        basis = request.query_params.get("basis") or "nominal"
        if as_of:
            from portfolio.services.valuation import value_as_of
            res = value_as_of(request.user, account=account, as_of=as_of, basis=basis)
            return Response({
                "id": account.id,
                "name": account.name,
                **res
            })

        result = value_account(account)
        result["prices"] = get_latest_prices()
        result = _with_usd(result)
        if basis == "usd_real":
            result = _express_usd_real(result)
        return Response({
            "id": account.id,
            "name": account.name,
            **result,
        })


def _scope(request):
    """Resolve the active portfolio from `?account=<id>`, owned by the user.

    Returns the Account or None. None means "all portfolios" only when the
    parameter is absent; invalid or unowned ids are explicit client errors.
    """
    raw = request.query_params.get("account")
    if not raw:
        return None
    try:
        account_id = int(raw)
    except (TypeError, ValueError):
        raise ValidationError("account must be an integer id.")
    account = request.user.accounts.filter(pk=account_id).first()
    if account is None:
        raise NotFound("Account not found.")
    return account


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


def _express_usd_real(valuation: dict) -> dict:
    """Re-express a live toman valuation in USD using the live USD cash rate."""
    prices = valuation.get("prices", {})
    usd_rate = Decimal(prices.get("usd_cash", 0) or 0)
    if usd_rate <= 0:
        valuation["basis"] = "usd_real"
        return valuation

    def _scale_items(items):
        for item in items or []:
            if item.get("value") is not None:
                item["value"] = float(Decimal(str(item["value"])) / usd_rate)
            if item.get("unit_price") is not None:
                item["unit_price"] = float(Decimal(str(item["unit_price"])) / usd_rate)

    total = Decimal(str(valuation.get("total", 0) or 0)) / usd_rate
    valuation["total"] = total
    valuation["total_usd"] = total
    _scale_items(valuation.get("items"))
    for account in valuation.get("accounts", []) or []:
        if account.get("total") is not None:
            account["total"] = Decimal(str(account["total"])) / usd_rate
        _scale_items(account.get("items"))
    valuation["basis"] = "usd_real"
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
        now = timezone.now()
        since = now - timedelta(days=days)

        # Choose grid step size based on days requested
        if days <= 7:
            step_minutes = 2
        elif days <= 30:
            step_minutes = 30
        else:
            step_minutes = 1440  # 1 day

        rows = list(snapshots.order_by("timestamp").values("timestamp", "total_value_tomans", "is_estimated"))
        
        valid_rows = []
        for r in rows:
            val_toman = Decimal(r["total_value_tomans"])
            if val_toman <= 0:
                continue
            valid_rows.append((r["timestamp"], val_toman, r.get("is_estimated", False)))

        # If no snapshot rows exist, fallback to live calculation total
        if not valid_rows:
            fallback_val = value_account(account)["total"] if account else value_user(request.user)["total"]
            if fallback_val > 0:
                valid_rows = [(since, fallback_val, False)]

        # Generate regular time grid slots from the first known value to now.
        series = []
        curr = valid_rows[0][0] if valid_rows else since
        step = timedelta(minutes=step_minutes)
        row_idx = 0
        last_val = Decimal("0")
        last_estimated = False

        while curr <= now + timedelta(seconds=10):
            while row_idx < len(valid_rows) and valid_rows[row_idx][0] <= curr:
                last_val = valid_rows[row_idx][1]
                last_estimated = valid_rows[row_idx][2]
                row_idx += 1
            
            val_usd = str(round(last_val / usd_rate, 2)) if usd_rate > 0 else None
            series.append({
                "timestamp": curr.isoformat(),
                "date": curr.strftime("%Y-%m-%d"),
                "time": curr.strftime("%H:%M"),
                "total": str(last_val),
                "total_usd": val_usd,
                "is_estimated": last_estimated,
            })
            curr += step
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
        try:
            limit = int(request.query_params.get("limit", "100"))
        except (TypeError, ValueError):
            return Response({"detail": "limit must be an integer."}, status=400)
        limit = min(max(limit, 1), 500)
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
        corr = df.corr().fillna(0.0)
        return Response({
            "assets": assets,
            "dates": dates,
            "returns": returns_payload,
            "correlation": {
                "assets": assets,
                "matrix": np.nan_to_num(corr.to_numpy(), nan=0.0).tolist(),
            },
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


class BacktestView(APIView):
    """Pro-tier: Create new walk-forward simulation runs and list runs."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        from portfolio.models import BacktestRun
        from portfolio.serializers import BacktestRunSerializer

        runs = BacktestRun.objects.filter(user=request.user)
        serializer = BacktestRunSerializer(runs, many=True)
        return Response(serializer.data)

    def post(self, request):
        from django.conf import settings
        from portfolio.models import BacktestRun, BacktestUserQuota
        from portfolio.serializers import BacktestRunSerializer
        from portfolio.tasks import run_backtest_task
        import hashlib
        import json

        today = timezone.now().date()
        limit = getattr(settings, "DAILY_BACKTEST_LIMIT", 10)
        quota, _ = BacktestUserQuota.objects.get_or_create(user=request.user, day=today)
        if quota.count >= limit:
            return Response(
                {"detail": f"Daily backtest limit of {limit} runs exceeded."},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        quota.count += 1
        quota.save()

        basis = request.data.get("basis", "nominal")
        universe = request.data.get("universe")

        # Stable universe hash
        if universe is None:
            univ_str = "default"
        else:
            univ_str = hashlib.md5(",".join(sorted(universe)).encode("utf-8")).hexdigest()[:16]

        params_hash = hashlib.md5(
            json.dumps({"basis": basis, "universe": universe}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

        run = BacktestRun.objects.create(
            user=request.user,
            basis=basis,
            universe=universe,
            universe_hash=univ_str,
            params_hash=params_hash,
            status=BacktestRun.Status.QUEUED,
        )

        run_backtest_task.delay(run.id)
        return Response(BacktestRunSerializer(run).data, status=status.HTTP_201_CREATED)


class BacktestDetailView(APIView):
    """Pro-tier: Retrieve a specific run status and results."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request, pk):
        from portfolio.models import BacktestRun
        from portfolio.serializers import BacktestRunSerializer

        run = BacktestRun.objects.filter(user=request.user, pk=pk).first()
        if not run:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = BacktestRunSerializer(run)
        return Response(serializer.data)


class DiscoveryView(APIView):
    """Pro-tier: Recommends candidates not currently held along with risk-adjusted leaders."""

    permission_classes = [IsAuthenticated, IsPro]

    def get(self, request):
        from marketdata.universe import get_candidate_universe
        from portfolio.services.returns import daily_returns_matrix
        from marketdata.models import MarketInstrument
        from portfolio.services.diagnostics import RISK_FREE_RATE_ANNUAL
        import numpy as np

        candidates, excluded = get_candidate_universe()

        # Fetch returns
        ret_nom, _ = daily_returns_matrix(universe=candidates, basis="nominal")
        ret_usd, _ = daily_returns_matrix(universe=candidates, basis="usd_real")

        leaders = {
            "nominal": {},
            "usd_real": {}
        }

        # Resolve candidate classes
        instruments = {mi.symbol: mi for mi in MarketInstrument.objects.filter(symbol__in=candidates)}

        def calc_risk_adj(df):
            res = {}
            for col in df.columns:
                series = df[col]
                if series.empty or series.std() == 0:
                    continue
                mean_ann = float(series.mean() * 252)
                vol_ann = float(series.std() * np.sqrt(252))
                sharpe = (mean_ann - RISK_FREE_RATE_ANNUAL) / vol_ann if vol_ann > 0 else 0.0

                # Downside dev for Sortino
                neg = series[series < 0]
                downside_vol = float(neg.std() * np.sqrt(252)) if not neg.empty else 0.0
                sortino = (mean_ann - RISK_FREE_RATE_ANNUAL) / downside_vol if downside_vol > 0 else 0.0

                # Max drawdown for Calmar
                cum = (1 + series).cumprod()
                running_max = cum.cummax()
                drawdowns = (cum - running_max) / running_max
                max_dd = float(abs(drawdowns.min()))
                calmar = mean_ann / max_dd if max_dd > 0 else 0.0

                mi = instruments.get(col)
                cat = "Other"
                if mi:
                    if mi.category == MarketInstrument.Category.STOCK:
                        cat = "Stock"
                    elif mi.category == MarketInstrument.Category.GOLD:
                        cat = "Gold"
                    else:
                        cat = "Cash"

                res.setdefault(cat, []).append({
                    "symbol": col,
                    "name": mi.name if mi else col,
                    "sharpe": sharpe,
                    "sortino": sortino,
                    "calmar": calmar,
                    "expected_return_annual": mean_ann,
                    "volatility_annual": vol_ann,
                })
            for cat in res:
                res[cat].sort(key=lambda x: x["sharpe"], reverse=True)
            return res

        if not ret_nom.empty:
            leaders["nominal"] = calc_risk_adj(ret_nom)
        if not ret_usd.empty:
            leaders["usd_real"] = calc_risk_adj(ret_usd)

        return Response({
            "candidates": candidates,
            "excluded": excluded,
            "leaders": leaders,
        })


class WatchlistView(APIView):
    """Manage watchlist items with force-include and force-exclude flags."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from portfolio.models import Watchlist
        from portfolio.serializers import WatchlistSerializer

        account = _scope(request) or request.user.accounts.first()
        if not account:
            return Response({"detail": "User has no accounts."}, status=status.HTTP_400_BAD_REQUEST)
        watchlist, _ = Watchlist.objects.get_or_create(account=account)
        serializer = WatchlistSerializer(watchlist)
        return Response(serializer.data)

    def post(self, request):
        from portfolio.models import Watchlist, WatchlistItem
        from portfolio.serializers import WatchlistItemSerializer

        account = _scope(request) or request.user.accounts.first()
        if not account:
            return Response({"detail": "User has no accounts."}, status=status.HTTP_400_BAD_REQUEST)
        watchlist, _ = Watchlist.objects.get_or_create(account=account)

        symbol = request.data.get("symbol")
        if not symbol:
            return Response({"detail": "symbol is required."}, status=status.HTTP_400_BAD_REQUEST)

        if request.data.get("delete", False):
            WatchlistItem.objects.filter(watchlist=watchlist, symbol=symbol).delete()
            return Response({"detail": "Watchlist item deleted."})

        force_include = request.data.get("force_include", False)
        force_exclude = request.data.get("force_exclude", False)

        item, _ = WatchlistItem.objects.get_or_create(watchlist=watchlist, symbol=symbol)
        item.force_include = force_include
        item.force_exclude = force_exclude
        item.save()

        return Response(WatchlistItemSerializer(item).data)


class PerformanceView(APIView):
    """Calculate and return Time-Weighted Return (TWR) and Money-Weighted Return (XIRR)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from portfolio.models import Account, Transaction, Snapshot
        from portfolio.services.timeline import asset_metrics, xirr, twr
        from portfolio.services.valuation import get_latest_prices, value_user
        from django.utils import timezone

        accounts = Account.objects.filter(user=request.user)
        txns = Transaction.objects.filter(account__in=accounts).order_by("timestamp")

        latest_prices = get_latest_prices()
        assets_summary = {}
        total_cost_basis = Decimal("0")
        total_realized_pnl = Decimal("0")
        total_unrealized_pnl = Decimal("0")

        txns_by_asset = {}
        for tx in txns:
            txns_by_asset.setdefault(tx.asset, []).append(tx)

        for asset, asset_txns in txns_by_asset.items():
            curr_price = Decimal(str(latest_prices.get(asset.key, 0)))
            metrics = asset_metrics(asset_txns, curr_price)
            assets_summary[asset.key] = {
                "asset_name": asset.name,
                "asset_class": asset.asset_class,
                "cost_basis": float(metrics["cost_basis"]),
                "realized_pnl": float(metrics["realized_pnl"]),
                "unrealized_pnl": float(metrics["unrealized_pnl"]),
                "quantity": float(metrics["quantity"]),
            }
            total_cost_basis += metrics["cost_basis"]
            total_realized_pnl += metrics["realized_pnl"]
            total_unrealized_pnl += metrics["unrealized_pnl"]

        # Calculate XIRR cashflows
        cashflows = []
        for tx in txns:
            cf_val = Decimal(str(tx.quantity)) * Decimal(str(tx.price_tomans))
            val = -cf_val if tx.side == Transaction.Side.BUY else cf_val
            cashflows.append((tx.timestamp.date(), val))

        current_val = Decimal(str(value_user(request.user)["total"]))
        if current_val > 0:
            cashflows.append((timezone.now().date(), current_val))

        user_xirr = xirr(cashflows) if cashflows else 0.0

        # Calculate TWR from snapshots
        snaps = Snapshot.objects.filter(user=request.user, account=None).order_by("timestamp")
        daily_vals = {}
        for s in snaps:
            daily_vals[s.timestamp.date()] = s.total_value_tomans

        dates = sorted(daily_vals.keys())
        periods = []
        for j in range(1, len(dates)):
            start_d = dates[j-1]
            end_d = dates[j]
            start_v = daily_vals[start_d]
            end_v = daily_vals[end_d]

            cf = Decimal("0")
            for tx in txns:
                if tx.timestamp.date() == end_d:
                    cf += Decimal(str(tx.quantity)) * Decimal(str(tx.price_tomans)) if tx.side == Transaction.Side.BUY else -Decimal(str(tx.quantity)) * Decimal(str(tx.price_tomans))
            periods.append((start_v, end_v, cf))

        user_twr = float(twr(periods)) if periods else 0.0

        return Response({
            "twr": user_twr,
            "xirr": user_xirr,
            "total_cost_basis": float(total_cost_basis),
            "total_realized_pnl": float(total_realized_pnl),
            "total_unrealized_pnl": float(total_unrealized_pnl),
            "assets_summary": assets_summary,
        })


class IntegrityView(APIView):
    """Retrieve symbols integrity quality metrics and rejected records."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_staff:
            return Response({"detail": "Staff only endpoint."}, status=status.HTTP_403_FORBIDDEN)

        from marketdata.models import SymbolIntegrity, RejectedRecord
        integrities = SymbolIntegrity.objects.all()
        rejected = RejectedRecord.objects.all().order_by("-occurrences")

        integrity_data = []
        for i in integrities:
            integrity_data.append({
                "symbol": i.symbol,
                "passes_gate": i.passes_gate,
                "coverage_ratio": float(i.coverage_ratio) if i.coverage_ratio else 0.0,
                "max_gap_days": i.max_gap_days,
                "reason": i.reason,
                "computed_at": i.computed_at.isoformat() if i.computed_at else None,
            })

        rejected_data = []
        for r in rejected:
            rejected_data.append({
                "id": r.id,
                "endpoint": r.endpoint,
                "symbol": r.symbol,
                "date": r.date,
                "reason": r.reason,
                "occurrences": r.occurrences,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            })

        return Response({
            "integrity": integrity_data,
            "rejected": rejected_data
        })

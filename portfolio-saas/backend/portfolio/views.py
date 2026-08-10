"""All portfolio endpoints: CRUD, live valuation, prices, and Pro analytics.

Valuation is computed live on read (holdings x latest prices) and the heavy
part (latest prices) is cached, so these endpoints stay cheap at scale.
Pro endpoints (insights/analytics/optimization) are gated by
RequiresFeature("<capability>"), resolved against the accounts.features registry.
"""
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from django.contrib.postgres.aggregates import BoolOr
from django.db.models import Avg
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import RequiresFeature

from .models import Account, Asset, Holding, LedgerEntry, Price, Snapshot, Transaction, Liability
from .serializers import (
    AccountSerializer,
    AssetSerializer,
    HoldingSerializer,
    LedgerEntryInputSerializer,
    LedgerEntrySerializer,
    TradeInputSerializer,
    TransactionSerializer,
    LiabilitySerializer,
)
from .services import execute_trade, get_latest_prices, undo_trade, value_account, value_user
from .services.valuation import (
    SYNTHETIC_HISTORY_MAX_DAYS,
    compute_dynamic_net_worth_series,
)
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
    MixedUnitUniverseBlocked,
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
        from .services.ledger import create_ledger_entry

        account = self._account()
        if account is None:
            raise NotFound("Account not found")
        if not serializer.validated_data["asset"].is_house:
            raise ValidationError("Tradeable assets must be changed through the buy/sell endpoint.")
        if account.holdings.filter(asset=serializer.validated_data["asset"]).exists():
            raise ValidationError("This asset already exists in the account.")
        data = serializer.validated_data
        create_ledger_entry(
            account=account,
            kind=LedgerEntry.Kind.OPENING_POSITION,
            asset=data["asset"],
            quantity=data["quantity"],
            area_sqm=data.get("area_sqm"),
            mortgage_deduction_tomans=data.get("mortgage_deduction_tomans"),
            occurred_at=account.tracking_started_at or timezone.now(),
            source="manual",
            note="Real-estate opening position",
        )
        serializer.instance = Holding.objects.get(
            account=account, asset=data["asset"]
        )


class HoldingDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = HoldingSerializer

    def get_queryset(self):
        return Holding.objects.filter(
            account__user=self.request.user,
            account_id=self.kwargs["account_id"],
        )

    def perform_update(self, serializer):
        from .services.ledger import house_ledger_entry, replace_ledger_entry

        if not serializer.instance.asset.is_house:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Tradeable assets must be changed through the buy/sell endpoint.")
        entry = house_ledger_entry(serializer.instance)
        data = serializer.validated_data
        replace_ledger_entry(
            user=self.request.user,
            account_id=serializer.instance.account_id,
            entry_id=entry.pk,
            quantity=data.get("quantity", serializer.instance.quantity),
            area_sqm=data.get("area_sqm", serializer.instance.area_sqm),
            mortgage_deduction_tomans=data.get(
                "mortgage_deduction_tomans",
                serializer.instance.mortgage_deduction_tomans,
            ),
        )
        serializer.instance = Holding.objects.get(
            account_id=self.kwargs["account_id"],
            asset_id=serializer.instance.asset_id,
        )

    def perform_destroy(self, instance):
        from .services.ledger import house_ledger_entry, reverse_ledger_entry

        if not instance.asset.is_house:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Tradeable assets must be changed through the buy/sell endpoint.")
        entry = house_ledger_entry(instance)
        reverse_ledger_entry(
            user=self.request.user,
            account_id=instance.account_id,
            entry_id=entry.pk,
        )


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
                area_sqm=data.get("area_sqm"),
                mortgage_deduction_tomans=data.get(
                    "mortgage_deduction_tomans"
                ),
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


class AccountDataQualityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, account_id):
        from marketdata.integrity import compute_symbol_integrity

        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            raise NotFound("Account not found.")
        assets = []
        for holding in account.holdings.select_related("asset"):
            asset = holding.asset
            symbol = asset.tse_symbol or asset.brs_symbol
            if asset.is_house or asset.is_manual or not symbol:
                assets.append({
                    "asset_key": asset.key,
                    "symbol": symbol or None,
                    "passes_gate": None,
                    "quality_status": "manual",
                    "reason_codes": ["manual_valuation"],
                })
                continue
            try:
                result = compute_symbol_integrity(
                    symbol,
                    start=request.query_params.get("from"),
                    end=request.query_params.get("to"),
                )
            except (TypeError, ValueError) as exc:
                return Response({"detail": str(exc)}, status=400)
            assets.append({"asset_key": asset.key, **result})

        assessed = [item for item in assets if item["passes_gate"] is not None]
        return Response({
            "account_id": account.id,
            "assets": assets,
            "passing_assets": sum(bool(item["passes_gate"]) for item in assessed),
            "assessed_assets": len(assessed),
            "quality_status": (
                "complete" if assessed and all(item["passes_gate"] for item in assessed)
                else "partial" if assessed
                else "unavailable"
            ),
            "known_limits": [
                {
                    "code": "no_iranian_holiday_calendar",
                    "detail": "Clock-based market state can poll on unobserved holidays until provider state refreshes.",
                },
                {
                    "code": "codal_page_cap",
                    "detail": "Codal verification is complete only to the configured five-page archive cap.",
                },
                {
                    "code": "market_state_cache_ttl",
                    "detail": "Provider market state is cached for 3600 seconds and has no external fallback.",
                },
            ],
        })


class TradeView(APIView):
    """Execute a buy/sell in one account (the ledger write path).

    POST /accounts/<id>/trades/  {asset_key, side, quantity, note?}
    Atomically appends a Transaction, updates the Holding balance, and snapshots
    net worth so the history chart steps at the trade moment.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, account_id):
        import logging
        logging.getLogger("django").warning(f"DEBUG VIEWS: TradeView request data: {request.data}")
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
            account__user=request.user, timestamp__gte=since,
            reversal_of__isnull=True, reversed_by__isnull=True
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
        basis = request.query_params.get("basis") or "nominal_toman"
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
        if basis in {"usd_real", "usd_denominated", "usdt_denominated"}:
            valuation = _express_usd_real(valuation, basis)
        return Response(valuation)


class AccountValuationView(APIView):
    """Current valuation for one account."""

    def get(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        as_of = request.query_params.get("as_of")
        basis = request.query_params.get("basis") or "nominal_toman"
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
        if basis in {"usd_real", "usd_denominated", "usdt_denominated"}:
            result = _express_usd_real(result, basis)
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


def _express_usd_real(valuation: dict, basis: str = "usd_denominated") -> dict:
    """Re-express a live toman valuation in USD or USDT using the live rate."""
    prices = valuation.get("prices", {})
    from portfolio.services.deflator import normalize_basis
    basis = normalize_basis(basis)
    
    rate_key = "usdt_irt" if basis == "usdt_denominated" else "usd_cash"
    rate = Decimal(prices.get(rate_key, 0) or 0)
    conversion_source = "USDT" if basis == "usdt_denominated" else "USD"
    
    if basis == "usdt_denominated" and rate <= 0:
        # Fallback to USD
        rate = Decimal(prices.get("usd_cash", 0) or 0)
        conversion_source = "USD"
        
    if rate <= 0:
        valuation["basis"] = basis
        valuation["conversion_source"] = conversion_source
        return valuation

    def _scale_items(items):
        for item in items or []:
            if item.get("value") is not None:
                item["value"] = float(Decimal(str(item["value"])) / rate)
            if item.get("unit_price") is not None:
                item["unit_price"] = float(Decimal(str(item["unit_price"])) / rate)

    total = Decimal(str(valuation.get("total", 0) or 0)) / rate
    valuation["total"] = total
    valuation["total_usd"] = total
    _scale_items(valuation.get("items"))
    for account in valuation.get("accounts", []) or []:
        if account.get("total") is not None:
            account["total"] = Decimal(str(account["total"])) / rate
        _scale_items(account.get("items"))
    valuation["basis"] = basis
    valuation["conversion_source"] = conversion_source
    return valuation


class SnapshotListView(APIView):
    """Per-user net-worth history for the FREE trend chart, plus trade markers.

    One point per calendar day: the average of every fetch snapshotted that day
    (fetches run every 2 minutes, so "today" is the running average of today's
    fetches so far). `?days=all` returns the full history. `trades` carries the
    buy/sell events in the same window so the chart can annotate the exact
    points where holdings changed.

    Holdings-only accounts (no BUY/SELL ledger) with thin Snapshot coverage get
    a warehouse-backed synthetic series capped at 90 days.

    `?account=<id>` scopes both the snapshot series and the trade markers to one
    portfolio (reads that account's per-account snapshot rows); absent = aggregate.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        raw_days = request.query_params.get("days", "30")
        show_all = raw_days == "all"
        if show_all:
            days = None
        else:
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                days = 30
            days = max(1, min(days, 3650))
        now = timezone.now()
        account = _scope(request)
        snapshots = Snapshot.objects.filter(
            user=request.user, total_value_tomans__gt=0
        )
        if not show_all:
            snapshots = snapshots.filter(timestamp__gte=now - timedelta(days=days))
        if account is not None:
            snapshots = snapshots.filter(account=account)
        else:
            snapshots = snapshots.filter(account=None)
        prices = get_latest_prices()
        usd_rate = Decimal(prices.get("usd_cash", 0) or 0)

        daily = list(
            snapshots
            .annotate(day=TruncDate("timestamp"))
            .values("day")
            .annotate(avg_total=Avg("total_value_tomans"), any_estimated=BoolOr("is_estimated"))
            .order_by("day")
        )

        accounts = [account] if account is not None else list(request.user.accounts.all())
        holdings_only = bool(accounts) and not LedgerEntry.objects.filter(
            account__in=accounts,
            kind__in=[LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL],
        ).exists()
        has_holdings = Holding.objects.filter(account__in=accounts).exists() if accounts else False

        short_window = not show_all and days <= SYNTHETIC_HISTORY_MAX_DAYS
        use_synthetic = (
            has_holdings
            and holdings_only
            and (len(daily) < days if short_window else len(daily) < 2)
        )

        if use_synthetic:
            synth_days = days if short_window else SYNTHETIC_HISTORY_MAX_DAYS
            dynamic = compute_dynamic_net_worth_series(
                request.user, account=account, days=synth_days
            )
            series = [
                {
                    "timestamp": row["date"],
                    "date": row["date"],
                    "total": row["total"],
                    "total_usd": row["total_usd"],
                    "is_estimated": True,
                }
                for row in dynamic
            ]
        else:
            # No snapshot rows yet (brand-new user) -> fall back to today's live total.
            if not daily:
                fallback_val = value_account(account)["total"] if account else value_user(request.user)["total"]
                if fallback_val > 0:
                    daily = [{"day": now.date(), "avg_total": fallback_val, "any_estimated": False}]

            series = []
            for row in daily:
                total = Decimal(row["avg_total"] or 0)
                val_usd = str(round(total / usd_rate, 2)) if usd_rate > 0 else None
                day_str = row["day"].strftime("%Y-%m-%d")
                series.append({
                    "timestamp": day_str,
                    "date": day_str,
                    "total": str(total),
                    "total_usd": val_usd,
                    "is_estimated": bool(row["any_estimated"]),
                })

        trades = (
            Transaction.objects.filter(
                account__user=request.user,
                asset__isnull=False,
                kind__in=[LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL],
            )
            .select_related("asset")
            .order_by("timestamp")
        )
        if not show_all:
            trades = trades.filter(timestamp__gte=now - timedelta(days=days))
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

    permission_classes = [IsAuthenticated, RequiresFeature("insights")]

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

    permission_classes = [IsAuthenticated, RequiresFeature("analytics")]

    def get(self, request):
        weights, total = _current_weights_and_total(request.user, _scope(request))
        return Response(
            portfolio_diagnostics(weights, total, user=request.user)
        )


class OptimizationView(APIView):
    """Pro-tier scenario optimizer: max_sharpe / min_volatility / risk_parity / hrp."""

    permission_classes = [IsAuthenticated, RequiresFeature("optimization")]

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
        except MixedUnitUniverseBlocked as exc:
            return Response(
                {
                    "detail": str(exc),
                    "tse_keys": exc.tse_keys,
                    "other_keys": exc.other_keys,
                    "policy": "docs/F1_POLICY.md",
                },
                status=409,
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

    permission_classes = [IsAuthenticated, RequiresFeature("frontier")]

    def get(self, request):
        weights, total = _current_weights_and_total(request.user, _scope(request))
        try:
            frontier = _efficient_frontier(n_points=30)
        except MixedUnitUniverseBlocked as exc:
            return Response(
                {
                    "detail": str(exc),
                    "tse_keys": exc.tse_keys,
                    "other_keys": exc.other_keys,
                    "policy": "docs/F1_POLICY.md",
                },
                status=409,
            )
        # Inject the current portfolio point.
        returns, _ = daily_returns_matrix()
        current_point = None
        cloud = []
        if weights and not returns.empty:
            cols = [k for k in weights if k in returns.columns]
            if cols:
                sub = returns[cols].fillna(0.0).to_numpy()
                w = np.array([weights[k] for k in cols], dtype=float)
                if w.sum() > 0:
                    w = w / w.sum()
                    port = pd.Series(sub @ w, index=returns.index)
                    if port.std(ddof=1) > 0:
                        ann_ret = float(port.mean() * 252)
                        ann_vol = float(port.std(ddof=1) * np.sqrt(252))
                        current_point = {
                            "return": _finite(ann_ret),
                            "volatility": _finite(ann_vol),
                            "weights": weights,
                        }
                # Random-weight cloud over the SAME held assets, so the chart shows
                # what varying the user's own mix (not the whole market) could do.
                # Pure numpy, no solver: 400 Dirichlet draws mapped through the
                # same covariance the frontier line already used.
                if len(cols) >= 2:
                    rng = np.random.default_rng()
                    draws = rng.dirichlet(np.ones(len(cols)), size=400)
                    port_returns = sub @ draws.T
                    means = port_returns.mean(axis=0) * 252
                    stds = port_returns.std(axis=0, ddof=1) * np.sqrt(252)
                    cloud = [
                        {"return": _finite(float(r)), "volatility": _finite(float(v))}
                        for r, v in zip(means, stds)
                        if v > 0
                    ]
        return Response({
            "frontier": frontier["frontier"],
            "max_sharpe": frontier["max_sharpe"],
            "min_volatility": frontier["min_volatility"],
            "current": current_point,
            "cloud": cloud,
        })


def _lifetime_days(user, account=None) -> int:
    """Days since tracking started -- the same inception source
    `account_performance()` uses, so "lifetime" agrees across pages."""
    if account is not None:
        start = account.tracking_started_at or (
            account.transactions.order_by("timestamp").values_list("timestamp", flat=True).first()
        )
    else:
        starts = [a.tracking_started_at for a in user.accounts.all() if a.tracking_started_at]
        start = min(starts) if starts else (
            LedgerEntry.objects.filter(account__user=user)
            .order_by("timestamp").values_list("timestamp", flat=True).first()
        )
    if start is None:
        return 365
    return max((timezone.now() - start).days, 30)


class MyOptimalView(APIView):
    """Pro-tier: "if a quant had optimized MY existing assets, what would it
    look like?" -- per lookback window, max-Sharpe and min-volatility weights
    over the user's OWN held assets, next to how the portfolio actually did.

    The math is exactly `optimize()` / `portfolio_diagnostics()`; this view is
    the window loop plus per-window error containment so a short-history user
    still sees their 1Y result even when 5Y/lifetime can't solve.
    """

    permission_classes = [IsAuthenticated, RequiresFeature("optimization")]

    WINDOWS = (("1Y", 365), ("3Y", 1095), ("5Y", 1825), ("Lifetime", None))

    def get(self, request):
        from .services.returns import get_universe_by_mode

        account = _scope(request)
        weights, total = _current_weights_and_total(request.user, account)
        if not weights:
            return Response({"detail": "No priced holdings to optimize yet."}, status=400)
        universe = get_universe_by_mode("held", user=request.user, account=account)
        lifetime_days = _lifetime_days(request.user, account)

        windows = []
        for label, fixed_days in self.WINDOWS:
            window_days = fixed_days or lifetime_days
            entry = {"label": label, "window_days": window_days}
            try:
                entry["max_sharpe"] = optimize(
                    scenario="max_sharpe", current_weights=weights, total_value_tomans=total,
                    user=request.user, history_days=window_days, universe=universe,
                )
            except (UniverseTooSmall, SolverError, NoAssetBeatsRiskFreeRate, MixedUnitUniverseBlocked) as exc:
                entry["status"] = "insufficient_history"
                entry["detail"] = str(exc)
                windows.append(entry)
                continue
            try:
                entry["min_volatility"] = optimize(
                    scenario="min_volatility", current_weights=weights, total_value_tomans=total,
                    user=request.user, history_days=window_days, universe=universe,
                )
            except (UniverseTooSmall, SolverError, MixedUnitUniverseBlocked):
                entry["min_volatility"] = None
            entry["actual"] = portfolio_diagnostics(
                weights, total, user=request.user, history_days=window_days, universe=universe,
            )
            # Historical drawdown for the hypothetical scenarios: "if you had
            # held these target weights fixed for the whole window" -- the same
            # portfolio_diagnostics() computation, just fed the target weights.
            for scenario_key in ("max_sharpe", "min_volatility"):
                scenario_payload = entry.get(scenario_key)
                if scenario_payload:
                    scenario_payload["diagnostics"] = portfolio_diagnostics(
                        scenario_payload["target_weights"], total,
                        user=request.user, history_days=window_days, universe=universe,
                    )
            entry["status"] = "ok"
            windows.append(entry)
        return Response({"windows": windows})


class AssetReturnsView(APIView):
    """Pro-tier daily-returns matrix + correlation, for heatmaps and scatter plots."""

    permission_classes = [IsAuthenticated, RequiresFeature("asset_returns")]

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


def _asset_class_leaders():
    """Top performers per asset class from the nightly AssetMetricSnapshot run.

    Only the 1-year window is populated today (nightly_asset_metrics' default),
    so this is independent of the window the user has selected on the page.
    """
    from marketdata.models import AssetMetricSnapshot, MarketInstrument
    from marketdata.universe import get_candidate_universe

    candidates, _ = get_candidate_universe()
    instruments = {mi.symbol: mi for mi in MarketInstrument.objects.filter(symbol__in=candidates)}
    snapshots = AssetMetricSnapshot.objects.filter(symbol__in=candidates, window_days=365)
    latest = snapshots.order_by("-as_of").values_list("as_of", flat=True).first()
    leaders = {}
    for row in snapshots.filter(as_of=latest).order_by("-sharpe"):
        instrument = instruments.get(row.symbol)
        category = instrument.get_category_display() if instrument else "Other"
        leaders.setdefault(category, []).append({
            "symbol": row.symbol,
            "name": instrument.name if instrument else row.symbol,
            "sharpe": row.sharpe,
            "sortino": row.sortino,
            "calmar": row.total_return / abs(row.max_drawdown) if row.max_drawdown else 0.0,
            "expected_return_annual": row.total_return,
            "volatility_annual": row.annualized_volatility,
        })
    return leaders, latest


class BestOverallView(APIView):
    """Pro-tier: "what is the best portfolio available across ALL tracked
    assets?" -- a pure read of the nightly `run_best_overall_snapshots`
    precompute (see `portfolio/services/best_overall.py`). No solver call in
    the request path; a window with no snapshot yet reports its own status
    rather than leaving the whole response empty.
    """

    permission_classes = [IsAuthenticated, RequiresFeature("discovery")]

    def get(self, request):
        from .optimization_models import OptimizationSnapshot
        from .services.best_overall import SCENARIOS, WINDOWS_DAYS

        window_labels = {365: "1Y", 1095: "3Y", 1825: "5Y", 3650: "10Y"}
        windows = []
        latest_created = None
        for window_days in WINDOWS_DAYS:
            entry = {"window_days": window_days, "label": window_labels.get(window_days, f"{window_days}d")}
            for scenario in SCENARIOS:
                snap = (
                    OptimizationSnapshot.objects
                    .filter(account=None, window_days=window_days, scenario=scenario)
                    .order_by("-created_at")
                    .first()
                )
                if snap is None:
                    entry[scenario] = None
                else:
                    entry[scenario] = snap.payload
                    if latest_created is None or snap.created_at > latest_created:
                        latest_created = snap.created_at
            entry["status"] = "ok" if (entry["max_sharpe"] or entry["min_volatility"]) else "insufficient_history"
            windows.append(entry)

        leaders, leaders_as_of = _asset_class_leaders()
        return Response({
            "windows": windows,
            "leaders": leaders,
            "leaders_as_of": leaders_as_of,
            "as_of": latest_created.isoformat() if latest_created else None,
        })


class AssetRankingView(APIView):
    permission_classes = [IsAuthenticated, RequiresFeature("asset_ranking")]

    def get(self, request):
        from marketdata.models import AssetMetricSnapshot

        account = _scope(request)
        if account is None:
            return Response({"detail": "account query param is required."}, status=400)
        symbols = [
            holding.asset.tse_symbol or holding.asset.brs_symbol
            for holding in account.holdings.select_related("asset")
            if holding.asset.tse_symbol or holding.asset.brs_symbol
        ]
        rows = AssetMetricSnapshot.objects.filter(
            symbol__in=symbols, window_days=365
        )
        latest = rows.order_by("-as_of").values_list("as_of", flat=True).first()
        return Response([
            {
                "symbol": row.symbol,
                "sharpe": row.sharpe,
                "sortino": row.sortino,
                "total_return": row.total_return,
                "annualized_volatility": row.annualized_volatility,
                "max_drawdown": row.max_drawdown,
            }
            for row in rows.filter(as_of=latest).order_by("-sharpe")
        ])


class PerformanceView(APIView):
    """One-release compatibility wrapper for account-scoped performance."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = _scope(request)
        if account is None:
            return Response(
                {"detail": "account query param is required."}, status=400
            )
        try:
            return Response(
                account_performance(
                    account, basis=request.query_params.get("basis")
                )
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)


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


class LiabilityListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LiabilitySerializer

    def get_queryset(self):
        return Liability.objects.filter(
            account__user=self.request.user,
            account_id=self.kwargs["account_id"],
        )

    def perform_create(self, serializer):
        account = get_object_or_404(
            self.request.user.accounts, pk=self.kwargs["account_id"]
        )
        serializer.save(account=account)


class LiabilityDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LiabilitySerializer

    def get_queryset(self):
        return Liability.objects.filter(
            account__user=self.request.user,
            account_id=self.kwargs["account_id"],
        )


class BrsApiWebhookView(APIView):
    """Simple authenticated webhook endpoint for brsapi.ir to notify of new prices.

    Expected usage: the brsapi system POSTs a small JSON body (e.g. {"symbols": [..]})
    and a header `X-BRS-WEBHOOK-SECRET` containing the shared secret defined in
    Django settings as `BRS_WEBHOOK_SECRET`. The endpoint enqueues a Celery task
    to compute and persist optimization snapshots.
    """

    # For the MVP we keep this permissive but require the configured secret.
    def post(self, request):
        from django.conf import settings
        secret = getattr(settings, "BRS_WEBHOOK_SECRET", None)
        if not secret:
            return Response({"detail": "Webhook secret not configured."}, status=400)
        header = request.headers.get("X-BRS-WEBHOOK-SECRET") or request.META.get("HTTP_X_BRS_WEBHOOK_SECRET")
        if not header or header != secret:
            return Response({"detail": "Unauthorized."}, status=401)

        # Optionally the payload can contain symbols or metadata; keep it for the task
        payload = request.data if request.data else {"trigger": "brs_webhook"}
        # Enqueue the optimization snapshot task
        try:
            from .tasks import run_global_optimization_snapshot
            run_global_optimization_snapshot.delay(payload)
        except Exception as exc:
            return Response({"detail": f"Failed to enqueue task: {exc}"}, status=500)

        return Response({"ok": True}, status=202)


class OptimizationSnapshotListView(APIView):
    """List optimization snapshots for an account or global snapshots.

    Query params:
      - account_id (optional): integer. If provided, must belong to the requesting user.
      - limit (optional): integer, default 20
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .optimization_models import OptimizationSnapshot
        from .serializers import OptimizationSnapshotSerializer
        from django.shortcuts import get_object_or_404

        account_id = request.query_params.get("account_id")
        limit = min(int(request.query_params.get("limit", 20)), 200)

        qs = OptimizationSnapshot.objects.all().order_by("-created_at")
        if account_id:
            # Verify ownership
            account = get_object_or_404(Account, pk=account_id, user=request.user)
            qs = qs.filter(account=account)
        else:
            # Only return global snapshots (account is null) or any snapshots if user is staff
            if not request.user.is_staff:
                qs = qs.filter(account__isnull=True)

        snaps = qs[:limit]
        serializer = OptimizationSnapshotSerializer(snaps, many=True)
        return Response(serializer.data)


class OptimizationSnapshotLatestView(APIView):
    """Return the latest snapshot for a given account (or global).

    Query params: account_id (optional)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .optimization_models import OptimizationSnapshot
        from .serializers import OptimizationSnapshotSerializer
        from django.shortcuts import get_object_or_404
        from django.db.models import Q

        account_id = request.query_params.get("account_id")
        if account_id:
            account = get_object_or_404(Account, pk=account_id, user=request.user)
            snap = OptimizationSnapshot.objects.filter(account=account).order_by("-created_at").first()
        else:
            # latest global snapshot
            if not request.user.is_staff:
                return Response({"detail": "Not found."}, status=404)
            snap = OptimizationSnapshot.objects.filter(account__isnull=True).order_by("-created_at").first()

        if not snap:
            return Response({"detail": "Not found."}, status=404)
        serializer = OptimizationSnapshotSerializer(snap)
        return Response(serializer.data)

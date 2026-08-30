"""All portfolio endpoints: CRUD, live valuation, prices, and analytics.

Valuation is computed live on read (holdings x latest prices) and the heavy
part (latest prices) is cached, so these endpoints stay cheap at scale.
Every endpoint requires authentication only — the FREE/PRO tier gating these
docs used to describe was removed along with the subscription model.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from django.conf import settings
from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


from .models import Account, Asset, Holding, LedgerEntry, Price, Snapshot, Transaction, Liability
from .serializers import (
    AccountSerializer,
    AssetSerializer,
    HoldingSerializer,
    LedgerEntryInputSerializer,
    LedgerEntryPatchSerializer,
    LedgerEntrySerializer,
    TradeInputSerializer,
    TransactionSerializer,
    LiabilitySerializer,
)
from .services import execute_trade, get_latest_prices, undo_trade, value_account, value_user
from .services.valuation import (
    HIDDEN_ADJUSTMENT_MAX_DAYS,
    SYNTHETIC_HISTORY_MAX_DAYS,
    compute_dynamic_net_worth_series,
)
from .services.visibility import hidden_asset_ids
from .services.trades import TradeError
from .services.ledger import (
    LedgerError,
    create_ledger_entry,
    delete_ledger_entry,
    delete_orphan_holding,
    entry_pnl_map,
    record_existing_position,
    reverse_ledger_entry,
    set_orphan_holding,
    synthetic_position_rows,
    update_ledger_entry,
)
from .services.imports import (
    LedgerImportError,
    commit_ledger_import,
    preview_ledger_import,
)
from .services.catalog import ensure_asset, search_catalog
from .services.deflator import cpi_for_date, normalize_basis
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
    """The shared asset catalog, plus this user's own properties."""

    serializer_class = AssetSerializer

    def get_queryset(self):
        return Asset.objects.filter(is_active=True).filter(
            Q(owner__isnull=True) | Q(owner=self.request.user)
        )


class AssetCatalogView(APIView):
    """Search the market catalog for the add-holding wizard."""

    def get(self, request):
        return Response(
            search_catalog(
                asset_class=request.query_params.get("asset_class", ""),
                q=request.query_params.get("q", ""),
                user=request.user,
            )
        )


class EnsureAssetView(APIView):
    """Mint (or reuse) a shared Asset for an eligible catalog instrument."""

    def post(self, request):
        asset = ensure_asset(
            source=request.data.get("source", ""),
            symbol=request.data.get("symbol", ""),
        )
        return Response(AssetSerializer(asset).data)


class AccountListCreateView(generics.ListCreateAPIView):
    serializer_class = AccountSerializer

    def get_queryset(self):
        return self.request.user.accounts.all().prefetch_related("holdings__asset")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class AccountDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = AccountSerializer

    def get_queryset(self):
        return self.request.user.accounts.all()


def _mint_property_asset(user, name: str) -> Asset:
    """Create a catalog row this user owns, for one property.

    Real estate is the only asset a user mints. A property is not a market
    instrument -- there is no ticker to point at and no other user's portfolio it
    belongs in -- but `Holding` is unique per (account, asset), so holding three
    properties needs three rows. Keys are random rather than derived from the
    name: two properties may legitimately share a name, and the key is an
    internal join, never something the owner reads (that is `Holding.label`).
    """
    from uuid import uuid4

    return Asset.objects.create(
        key=f"re-{uuid4().hex[:12]}",
        name=name[:120],
        asset_class=Asset.AssetClass.REAL_ESTATE,
        currency=Asset.Currency.IRT,
        is_house=True,
        is_active=True,
        owner=user,
    )


def _apply_presentation_fields(holding: Holding, data: dict) -> None:
    """Persist the nickname and the visibility tick.

    These describe how a holding is *shown*, not what happened to it, so they are
    written straight to the row. Routing them through the ledger would append a
    revaluation mark every time someone renamed a property or unticked it.
    """
    fields = [f for f in ("display_name", "is_hidden") if f in data]
    if not fields:
        return
    for field in fields:
        setattr(holding, field, data[field])
    holding.save(update_fields=[*fields, "updated_at"])


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
        from .services.ledger import (
            LedgerError,
            backfill_house_into_snapshots,
            create_ledger_entry,
            record_house_mark,
            record_manual_price,
            set_orphan_holding,
        )

        account = self._account()
        if account is None:
            raise NotFound("Account not found")
        data = serializer.validated_data
        property_name = data.pop("new_property_name", "")
        if property_name and not data.get("asset"):
            data["asset"] = _mint_property_asset(self.request.user, property_name)
        if account.holdings.filter(asset=data["asset"]).exists():
            raise ValidationError("This asset already exists in the account.")
        try:
            if data["asset"].is_house:
                record_house_mark(
                    user=self.request.user,
                    account_id=account.id,
                    asset=data["asset"],
                    quantity=data["quantity"],
                    area_sqm=data.get("area_sqm"),
                    mortgage_deduction_tomans=data.get("mortgage_deduction_tomans"),
                    occurred_at=data.get("occurred_at"),
                )
                holding = Holding.objects.get(account=account, asset=data["asset"])
                backfill_house_into_snapshots(
                    account, data["asset"], before=holding.created_at,
                )
            elif data["asset"].is_manual:
                set_orphan_holding(
                    account=account, asset=data["asset"], quantity=data["quantity"]
                )
                record_manual_price(data["asset"], data.get("unit_price_tomans"))
            else:
                # Always a purchase now, whether or not the portfolio tracks cash.
                # It used to branch: an account with no cash recorded the bare
                # position instead, because a BUY would have been rejected for
                # insufficient funds. `Account.track_cash` removed that failure,
                # so every acquisition can be the dated, priced event it really
                # is -- which is also the only way it gets a cost basis.
                create_ledger_entry(
                    account=account,
                    kind=LedgerEntry.Kind.BUY,
                    asset=data["asset"],
                    quantity=data["quantity"],
                    unit_price_tomans=data.get("unit_price_tomans"),
                    source="manual",
                    note="Dashboard holding opening trade",
                )
        except LedgerError as exc:
            raise ValidationError(str(exc)) from exc
        instance = Holding.objects.get(account=account, asset=data["asset"])
        _apply_presentation_fields(instance, data)
        serializer.instance = instance


class HoldingDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = HoldingSerializer

    def get_queryset(self):
        return Holding.objects.filter(
            account__user=self.request.user,
            account_id=self.kwargs["account_id"],
        )

    def perform_update(self, serializer):
        from .services.ledger import (
            LedgerError,
            adjust_holding_quantity,
            record_house_mark,
            update_manual_holding,
        )

        asset = serializer.instance.asset
        data = serializer.validated_data
        # Presentation first and unconditionally: a rename or a visibility toggle
        # must not fall through into a ledger write, and may arrive on its own.
        _apply_presentation_fields(serializer.instance, data)
        if not any(k in data for k in ("quantity", "unit_price_tomans", "area_sqm",
                                       "mortgage_deduction_tomans")):
            return
        if asset.is_manual and not asset.is_house and not LedgerEntry.objects.filter(
            account=serializer.instance.account, asset=asset
        ).exists():
            try:
                serializer.instance = update_manual_holding(
                    serializer.instance,
                    quantity=data.get("quantity", serializer.instance.quantity),
                    unit_price_tomans=data.get("unit_price_tomans"),
                )
            except LedgerError as exc:
                raise ValidationError(str(exc)) from exc
            return
        if not asset.is_house:
            try:
                serializer.instance = adjust_holding_quantity(
                    user=self.request.user,
                    account_id=serializer.instance.account_id,
                    holding_id=serializer.instance.id,
                    quantity=data.get("quantity", serializer.instance.quantity),
                    unit_price_tomans=data.get("unit_price_tomans"),
                )
            except LedgerError as exc:
                raise ValidationError(str(exc)) from exc
            return
        # Append a dated mark rather than rewriting the opening entry, so the
        # house's history shows what it was worth at the time instead of being
        # retro-priced at today's figure. `occurred_at` lets the client date a
        # revaluation it is entering after the fact.
        # Every other branch here translates a LedgerError into a 400 that names
        # the rule; this one did not, so a rejected mark reached the client as an
        # unexplained 500 ("Something went wrong").
        try:
            record_house_mark(
                user=self.request.user,
                account_id=serializer.instance.account_id,
                asset=serializer.instance.asset,
                quantity=data.get("quantity", serializer.instance.quantity),
                area_sqm=data.get("area_sqm", serializer.instance.area_sqm),
                mortgage_deduction_tomans=data.get(
                    "mortgage_deduction_tomans",
                    serializer.instance.mortgage_deduction_tomans,
                ),
                occurred_at=data.get("occurred_at"),
            )
        except LedgerError as exc:
            raise ValidationError(str(exc)) from exc
        serializer.instance = Holding.objects.get(
            account_id=self.kwargs["account_id"],
            asset_id=serializer.instance.asset_id,
        )

    def perform_destroy(self, instance):
        from .services.ledger import (
            LedgerError,
            adjust_holding_quantity,
            delete_orphan_holding,
            retire_house,
        )

        if not instance.asset.is_house and not LedgerEntry.objects.filter(
            account=instance.account, asset=instance.asset
        ).exists():
            try:
                delete_orphan_holding(
                    user=self.request.user,
                    account_id=instance.account_id,
                    holding_id=instance.id,
                )
            except LedgerError as exc:
                raise ValidationError(str(exc)) from exc
            return
        if not instance.asset.is_house:
            try:
                adjust_holding_quantity(
                    user=self.request.user,
                    account_id=instance.account_id,
                    holding_id=instance.id,
                    quantity=0,
                )
            except LedgerError as exc:
                raise ValidationError(str(exc)) from exc
            return
        try:
            retire_house(
                user=self.request.user,
                account_id=instance.account_id,
                holding=instance,
            )
        except LedgerError as exc:
            raise ValidationError(str(exc)) from exc


def _ledger_payload(user, account=None):
    accounts = user.accounts.all()
    if account is not None:
        accounts = accounts.filter(pk=account.pk)
    rows = list(
        LedgerEntry.objects.filter(
            account__in=accounts,
            reversal_of__isnull=True,
            reversed_by__isnull=True,
        )
        .select_related("asset", "account")
        .order_by("-timestamp", "-pk")
    )
    labels = {
        (h.account_id, h.asset_id): h.display_name
        for h in Holding.objects.filter(account__in=accounts).exclude(display_name="")
    }
    prices = get_latest_prices()
    data = list(
        LedgerEntrySerializer(
            rows,
            many=True,
            context={
                "pnl": entry_pnl_map(rows, prices),
                "labels": labels,
            },
        ).data
    )
    data.extend(synthetic_position_rows(accounts, rows, prices))
    return data


class LedgerListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def _account(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            raise NotFound("Account not found.")
        return account

    def get(self, request, account_id):
        account = self._account(request, account_id)
        return Response(_ledger_payload(request.user, account=account))

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
            # "I already own this" is an intent, not a kind: which row it
            # becomes depends on whether the date falls inside the tracked
            # window. See ledger.record_existing_position. Houses keep their own
            # path -- a property is a series of marks, not a position.
            if (
                data["kind"] == LedgerEntry.Kind.OPENING_POSITION
                and asset is not None
                and not asset.is_house
                # Real-estate fields on a non-house asset are a client error
                # that `create_ledger_entry` rejects by name. Routing around it
                # would answer 201 and drop them silently.
                and data.get("area_sqm") is None
                and data.get("mortgage_deduction_tomans") is None
            ):
                entry = record_existing_position(
                    account=account,
                    asset=asset,
                    quantity=data.get("quantity"),
                    unit_price_tomans=data.get("unit_price_tomans"),
                    occurred_at=data["occurred_at"],
                    source=data["source"],
                    note=data.get("note", ""),
                    external_id=data.get("external_id", ""),
                )
                return Response(LedgerEntrySerializer(entry).data, status=201)
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


class LedgerIndexView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_ledger_payload(request.user))


class LedgerEntryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, account_id, entry_id):
        form = LedgerEntryPatchSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        try:
            entry = update_ledger_entry(
                user=request.user, account_id=account_id, entry_id=entry_id,
                **form.validated_data,
            )
        except LedgerEntry.DoesNotExist:
            return Response({"detail": "Ledger entry not found."}, status=404)
        except LedgerError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(LedgerEntrySerializer(entry).data)

    def delete(self, request, account_id, entry_id):
        try:
            delete_ledger_entry(
                user=request.user, account_id=account_id, entry_id=entry_id
            )
        except LedgerEntry.DoesNotExist:
            return Response({"detail": "Ledger entry not found."}, status=404)
        except LedgerError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(status=204)


class LedgerPositionView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, account_id, holding_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            raise NotFound("Account not found.")
        holding = Holding.objects.filter(pk=holding_id, account=account).first()
        if holding is None:
            return Response({"detail": "Holding not found."}, status=404)
        quantity = request.data.get("quantity")
        try:
            set_orphan_holding(account=account, asset=holding.asset, quantity=quantity)
        except LedgerError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(_ledger_payload(request.user, account=account))

    def delete(self, request, account_id, holding_id):
        try:
            delete_orphan_holding(
                user=request.user, account_id=account_id, holding_id=holding_id
            )
        except Holding.DoesNotExist:
            return Response({"detail": "Holding not found."}, status=404)
        except LedgerError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(status=204)


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
        for holding in account.holdings.filter(is_hidden=False).select_related("asset"):
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

    permission_classes = [IsAdminUser]

    def get(self, request):
        stats = audit_and_repair_prices(fix=False)
        return Response(stats)


class AdminCleanPricesExecuteView(APIView):
    """Execute database cleanup: delete corrupted price rows, repair snapshots, and log action (Admin only).

    Destructive and irreversible (permanently deletes Price/Snapshot rows), so
    it requires the caller to echo back CONFIRM_PHRASE rather than firing on a
    bare POST — a single accidental click must not be enough to trigger it.
    """

    permission_classes = [IsAdminUser]
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
        days, _ = _int_param(request, "days", 90, clamp=(1, 3650), strict=False)
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
        elif basis == "real_toman":
            valuation = _express_real_toman(valuation)
        else:
            # Stamped on every path, not just the converted ones. The client keeps
            # the previous payload on screen while the next one loads, so a basis
            # switch briefly rendered Toman figures under a dollar sign -- reading
            # the basis off the DATA rather than off the picker keeps the number
            # and its currency describing the same thing.
            valuation["basis"] = "nominal_toman"
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
        elif basis == "real_toman":
            result = _express_real_toman(result)
        else:
            result["basis"] = "nominal_toman"
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


def _int_param(request, name, default, *, clamp=None, allowed=None, strict=True):
    """Read one integer query param; returns `(value, error_response_or_None)`.

    Nine views hand-rolled this and had already drifted apart -- `except
    ValueError` in one place and `except (TypeError, ValueError)` in the next,
    a silent fallback here and a 400 there. `strict=False` keeps the two
    endpoints that deliberately fall back to their default instead of failing.
    """
    try:
        value = int(request.query_params.get(name) or default)
    except (TypeError, ValueError):
        if strict:
            return None, Response({"detail": f"{name} must be an integer."}, status=400)
        value = default
    if allowed is not None and value not in allowed:
        options = ", ".join(str(a) for a in allowed[:-1])
        return None, Response(
            {"detail": f"{name} must be {options}, or {allowed[-1]}."}, status=400
        )
    if clamp:
        value = max(clamp[0], min(value, clamp[1]))
    return value, None


def _fx_rate(prices, basis):
    """The Toman-per-unit rate for a denominated basis, and which currency it was.

    USDT falls back to USD rather than refusing: a USDT-denominated view with no
    USDT quote is still answerable, and naming the rate actually used matters
    more than the request failing.
    """
    if basis == "usdt_denominated":
        rate = Decimal(prices.get("usdt_irt", 0) or 0)
        if rate > 0:
            return rate, "USDT"
    return Decimal(prices.get("usd_cash", 0) or 0), "USD"


def _rescale(valuation, factor, *, to_foreign_currency=False):
    """Divide every monetary field of a valuation payload by `factor`, in place.

    One walk for both re-expressions below (FX and CPI) -- they differ only in
    where the divisor comes from, and a second copy of this traversal is how a
    newly added money field ends up deflated on one basis but not the other.

    `to_foreign_currency` says the result is no longer denominated in Iranian
    money, which is the one case where a Rial-quoted TSE price has to be brought
    onto the Toman scale before the divide. Deflating to constant Tomans does
    not: real Rial is still Rial, and the label stays honest.
    """
    def scale_items(items):
        for item in items or []:
            # A TSE quote is Rial while its `value` is Toman -- the division lands
            # on the product, never the price. Dividing that Rial price straight
            # by an FX rate produces a "dollar" price ten times too big, so
            # `quantity x unit_price` came out at ten times the `value` beside it:
            # the very mismatch the Rial label was added to remove, moved onto the
            # foreign bases. Normalise the price to Toman FIRST, then convert, and
            # say that it is no longer Rial.
            from marketdata.currency import TSE_RIAL_PER_TOMAN

            if to_foreign_currency and item.get("unit_price_currency") == "rial":
                if item.get("unit_price") is not None:
                    item["unit_price"] = float(
                        Decimal(str(item["unit_price"])) / TSE_RIAL_PER_TOMAN
                    )
                item["unit_price_currency"] = "toman"
            # `price_per_sqm_tomans` is money too: a property left in Toman while
            # its own value column converted would read as an absurd unit price.
            for field in ("value", "unit_price", "price_per_sqm_tomans"):
                if item.get(field) is not None:
                    item[field] = float(Decimal(str(item[field])) / factor)

    def scale_liabilities(rows):
        # `total_liabilities` below is the sum of exactly these rows. Converting
        # the sum and not its addends is the same trap one line further down,
        # one level deeper: an itemised debt list that does not add up to the
        # total printed above it, in a payload that has declared its basis.
        # `value_user` rebuilds these as fresh dicts per account, so the root
        # list and the per-account lists are separate objects and each is
        # divided exactly once.
        for row in rows or []:
            if row.get("amount_tomans") is not None:
                row["amount_tomans"] = float(
                    Decimal(str(row["amount_tomans"])) / factor
                )

    valuation["total"] = Decimal(str(valuation.get("total", 0) or 0)) / factor
    if valuation.get("total_usd") is not None:
        valuation["total_usd"] = Decimal(str(valuation["total_usd"])) / factor
    # Debt is money. It is already netted out of `total`, so leaving it in Toman
    # only shows up when something reads the field on its own -- which is exactly
    # the "deflated on one basis but not the other" trap this single walk exists
    # to close, and it would report a mortgage at 42,000x under a dollar basis.
    if valuation.get("total_liabilities") is not None:
        valuation["total_liabilities"] = float(
            Decimal(str(valuation["total_liabilities"])) / factor
        )
    # Switched-off rows are still displayed, so they are re-expressed alongside
    # the counted ones even though they are absent from the total.
    scale_items(valuation.get("items"))
    scale_items(valuation.get("hidden_items"))
    scale_liabilities(valuation.get("liabilities"))
    for account in valuation.get("accounts") or []:
        if account.get("total") is not None:
            account["total"] = Decimal(str(account["total"])) / factor
        # `value_user` puts a `total_liabilities` on every account as well as on
        # the root, so converting only the root left the aggregate debt in
        # dollars beside each account's debt in Toman, in one payload.
        if account.get("total_liabilities") is not None:
            account["total_liabilities"] = float(
                Decimal(str(account["total_liabilities"])) / factor
            )
        scale_items(account.get("items"))
        scale_items(account.get("hidden_items"))
        scale_liabilities(account.get("liabilities"))
    return valuation


def _express_usd_real(valuation: dict, basis: str = "usd_denominated") -> dict:
    """Re-express a live Toman valuation in USD or USDT using the live rate."""
    basis = normalize_basis(basis)
    rate, source = _fx_rate(valuation.get("prices", {}), basis)
    if rate > 0:
        _rescale(valuation, rate, to_foreign_currency=True)
        # Past this point the total *is* the USD/USDT figure.
        valuation["total_usd"] = valuation["total"]
        valuation["basis"] = basis
    else:
        # No rate -- currency fetch down, or a cold cache -- so nothing was
        # converted and the figures are still Toman. Stamping the requested
        # basis anyway was survivable while the client read the picker and was
        # wrong in the same direction; now that it trusts this field, saying
        # "usd_denominated" over Toman renders a 33-billion-Toman portfolio as
        # $33,600,000,000. The snapshot endpoint answers the same way.
        valuation["basis"] = "nominal_toman"
    valuation["conversion_source"] = source
    return valuation


def _express_real_toman(valuation: dict) -> dict:
    """Deflate a live Toman valuation by the CPI index for today.

    That index is an SCI release for verified years and an operator projection
    beyond them, so the payload carries `cpi_estimated_years` and `cpi_source`
    to say which was used. Never assume the number here is published data.
    """
    _rescale(valuation, Decimal(str(cpi_for_date(timezone.now()))) / Decimal("100"))
    valuation["basis"] = "real_toman"
    valuation["cpi_vintage_year"] = settings.CPI_VERIFIED_THROUGH_YEAR
    # The deflator may have used an estimated anchor. Say so rather than letting
    # a projected index pass for a published one.
    valuation["cpi_estimated_years"] = sorted(settings.CPI_ESTIMATED_YEARS)
    valuation["cpi_source"] = settings.CPI_SOURCE
    return valuation


def _subtract_hidden_holdings(user, account, series, now) -> None:
    """Net switched-off holdings out of a recorded net-worth series, in place.

    Stored snapshots are a faithful record of everything owned; the tick that
    hides an asset is a view preference applied at read time. Subtracting here --
    over the whole window rather than from the moment the box was unticked --
    is what keeps the line continuous instead of putting a cliff in it on the day
    the user changed their mind.

    The subtrahend comes from the same routine that draws the synthetic series,
    so both sides of the arithmetic resolve a past price identically. Days where
    a hidden asset had no real close and its live price stood in are marked
    `approximated` rather than silently adjusted, and the result is floored at
    zero: a stale snapshot paired with a since-appreciated property could
    otherwise subtract past the total and draw a negative net worth.

    Recomputation is bounded at HIDDEN_ADJUSTMENT_MAX_DAYS. Points older than
    that reuse the oldest adjustment that WAS computed rather than going
    unadjusted: leaving them alone would put the hidden asset back into the far
    end of the line and draw exactly the cliff this function exists to avoid,
    only at the bound instead of at the day the box was unticked. Carrying the
    value back is an estimate, and says so.
    """
    if not series:
        return
    accounts = [account] if account is not None else list(user.accounts.all())
    if not hidden_asset_ids(accounts):
        return
    earliest = min(row["date"] for row in series)
    span = (now.date() - datetime.strptime(earliest, "%Y-%m-%d").date()).days + 1
    span = max(1, min(span, HIDDEN_ADJUSTMENT_MAX_DAYS))
    hidden = compute_dynamic_net_worth_series(
        user, account=account, days=span, only_hidden=True, max_days=span
    )
    if not hidden:
        return
    # Both sides key on a "%Y-%m-%d" string: the snapshot rows via TruncDate, the
    # subtrahend via strftime on an aware datetime. They agree only because
    # settings.TIME_ZONE is UTC. Change that and this join silently misses,
    # leaving totals unadjusted (and flagged approximate) on the shifted days.
    by_date = {row["date"]: row for row in hidden}
    oldest = min(by_date)
    for row in series:
        adjustment = by_date.get(row["date"])
        beyond_reach = adjustment is None
        if beyond_reach:
            adjustment = by_date[oldest]
        snap_total = Decimal(row["total"])
        hidden_total = Decimal(adjustment["total"])
        # A photograph taken before those holdings existed cannot contain them.
        # Subtracting anyway floors every such day at zero -- the Aug 2026 hole.
        if snap_total < hidden_total:
            continue
        net = snap_total - hidden_total
        row["total"] = str(max(net, Decimal("0")))
        row["approximated"] = beyond_reach or bool(adjustment["approximated"])


def _cpi_window_provenance(series: list[dict]) -> dict:
    """The inflation this chart actually divided by, and where it came from.

    "After inflation" is a claim about a rate, and until now the rate itself
    never appeared: the reader saw two diverging lines and had to take the size
    of the gap on faith. A projection running at triple the published pace looks
    exactly like a projection running at the right one.

    `applied_annual_rate` is the pace implied by the CPI at the two ends of the
    drawn window, annualized -- not the configured knob, which is only one of
    the inputs (a window spanning Nowruz interpolates between two anchors, one
    of which may be a published figure). That is the number the line is made of,
    so that is the number to show.
    """
    provenance = {
        "source": settings.CPI_SOURCE,
        "base_jalali_year": min(settings.CPI_BY_JALALI_YEAR),
        "verified_through_jalali_year": settings.CPI_VERIFIED_THROUGH_YEAR,
        "estimated_jalali_years": sorted(settings.CPI_ESTIMATED_YEARS),
        "applied_annual_rate": None,
    }
    if len(series) < 2:
        return provenance
    first = datetime.strptime(series[0]["date"], "%Y-%m-%d").date()
    last = datetime.strptime(series[-1]["date"], "%Y-%m-%d").date()
    span_days = (last - first).days
    if span_days <= 0:
        return provenance
    start_cpi = cpi_for_date(first)
    end_cpi = cpi_for_date(last)
    if start_cpi > 0 and end_cpi > 0:
        provenance["applied_annual_rate"] = (end_cpi / start_cpi) ** (365.0 / span_days) - 1.0
    return provenance


class SnapshotListView(APIView):
    """Per-user net-worth history for the FREE trend chart, plus trade markers.

    One point per calendar day: the verified session-close snapshot when one
    exists, otherwise the latest non-estimated snapshot, or the latest estimated
    gap-fill when no live snapshot exists. `?days=all`
    returns the full history. `trades` carries the
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
        basis = request.query_params.get("basis") or "nominal_toman"
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

        # Use one representative observation per day. Averaging intraday
        # snapshots makes a closed-market chart disagree with the authoritative
        # session close and can turn a large final-price move into a misleading
        # portfolio cliff.
        daily = list(
            snapshots
            .annotate(day=TruncDate("timestamp"))
            .annotate(
                row_number=Window(
                    expression=RowNumber(),
                    partition_by=[TruncDate("timestamp")],
                    order_by=[
                        F("is_session_close").desc(),
                        F("is_estimated").asc(),
                        F("timestamp").desc(),
                    ],
                )
            )
            .filter(row_number=1)
            .values(
                "day",
                "total_value_tomans",
                "is_estimated",
                "is_session_close",
            )
            .order_by("day")
        )

        accounts = [account] if account is not None else list(request.user.accounts.all())
        holdings_only = bool(accounts) and not LedgerEntry.objects.filter(
            account__in=accounts,
            kind__in=[LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL],
        ).exists()
        has_holdings = Holding.objects.filter(account__in=accounts).exists() if accounts else False

        short_window = not show_all and days <= SYNTHETIC_HISTORY_MAX_DAYS
        # Only synthesise when there is genuinely nothing recorded to draw.
        # This used to compare the number of observed days against the number of
        # CALENDAR days in the window; markets are shut on Thursday and Friday,
        # so that comparison was permanently true for every holdings-only
        # account and threw away real snapshots on every single request in
        # favour of a recomputed estimate. Recorded history always wins.
        use_synthetic = has_holdings and holdings_only and len(daily) < 2

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
                    "is_session_close": False,
                }
                for row in dynamic
            ]
        else:
            # No snapshot rows yet (brand-new user) -> fall back to today's live
            # total. Hidden holdings included, so this row means the same thing as
            # the stored rows it stands in for and goes through the same
            # subtraction below rather than being netted twice.
            if not daily:
                fallback_val = (
                    value_account(account, include_hidden=True)["total"]
                    if account
                    else value_user(request.user, include_hidden=True)["total"]
                )
                if fallback_val > 0:
                    daily = [{
                        "day": now.date(),
                        "total_value_tomans": fallback_val,
                        "is_estimated": False,
                        "is_session_close": False,
                    }]

            series = []
            for row in daily:
                total = Decimal(row["total_value_tomans"] or 0)
                day_str = row["day"].strftime("%Y-%m-%d")
                series.append({
                    "timestamp": day_str,
                    "date": day_str,
                    "total": str(total),
                    "total_usd": None,
                    "is_estimated": bool(row["is_estimated"]),
                    "is_session_close": bool(row["is_session_close"]),
                })
            # Snapshots record everything owned, so anything switched off has to
            # come back out here -- across the whole window, not from today
            # forward, or the chart would step down on the day the box was
            # unticked. USD is derived after the subtraction for the same reason.
            _subtract_hidden_holdings(request.user, account, series, now)
            for row in series:
                total = Decimal(row["total"])
                row["total_usd"] = (
                    str(round(total / usd_rate, 2)) if usd_rate > 0 else None
                )

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
        # A denominated basis divides every point by one live rate; `real_toman`
        # divides each point by the CPI *of its own day*, which is the whole
        # point of a real series -- hence a per-row divisor rather than one.
        if basis in ("usd_denominated", "usdt_denominated"):
            fx_rate, _source = _fx_rate(prices, normalize_basis(basis))
            divisor = (lambda row: fx_rate) if fx_rate > 0 else None
        elif basis == "real_toman":
            divisor = lambda row: Decimal(  # noqa: E731
                str(cpi_for_date(row.get("date") or row.get("timestamp")))
            ) / Decimal("100")
        else:
            divisor = None
        if divisor:
            for row in series:
                for field in ("total", "total_usd"):
                    if row.get(field) is not None:
                        row[field] = float(Decimal(str(row[field])) / divisor(row))
        # The basis these points are actually IN, which is not always the one that
        # was asked for: with no FX rate available `divisor` is None and the series
        # stays in Toman, and the chart would have gone on labelling it dollars.
        # It also closes the same race the valuation payload carries this field
        # for -- the client keeps the previous series on screen while the next one
        # loads, so a basis switch drew Toman under a dollar axis until it landed.
        applied_basis = basis if divisor else "nominal_toman"
        payload = {
            "series": series,
            "trades": markers,
            "basis": applied_basis,
        }
        if applied_basis == "real_toman":
            payload["cpi"] = _cpi_window_provenance(series)
        return Response(payload)


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
        limit, error = _int_param(request, "limit", 100, clamp=(1, 500))
        if error:
            return error
        rows = (
            Price.objects.filter(asset__key=asset_key)
            .order_by("-fetched_at")[:limit]
        )
        return Response([
            {"price": float(r.price), "fetched_at": r.fetched_at.isoformat()}
            for r in rows
        ])


class InsightsView(APIView):
    """Rule-based financial insights for the requested scope."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(build_insights(request.user, _scope(request)))


def _current_weights_and_total(
    user, account=None
) -> tuple[dict[str, float], Decimal, dict]:
    """Liquid weights + liquid total + the valuation they came from.

    `account=None` analyzes the whole-user portfolio; passing an account scopes
    weights to that single portfolio.

    The valuation is returned rather than recomputed by each caller:
    `portfolio_diagnostics` derives its `held_keys` from it (see
    `diagnostics._aggregate_holdings`), and calling it without one silently
    yields an EMPTY held set, which puts the user's own holdings back under the
    market-universe screening gates.
    """
    valuation = value_account(account) if account is not None else value_user(user)
    items = _liquid_items(valuation)
    total = _total(items)
    if total <= 0:
        return {}, Decimal("0"), valuation
    # Sum across portfolios: `_liquid_items` flattens every account into one list,
    # so an asset held in two of them appears twice. Keying a dict comprehension on
    # `i["key"]` kept only the LAST row and silently discarded the rest, while
    # `total` still counted them -- the weights then summed to less than 1 and the
    # optimizer rebalanced a book it believed was smaller than it is. On the family
    # account that hid 580,300,000 T (2.4%) held as usd_cash and quarter_coin in
    # both portfolios, and made every rebalance plan buy more than it sold.
    by_key: dict[str, Decimal] = {}
    for i in items:
        if i["value"] > 0:
            by_key[i["key"]] = by_key.get(i["key"], Decimal("0")) + i["value"]
    weights = {key: float(value / total) for key, value in by_key.items()}
    return weights, total, valuation


class AnalyticsView(APIView):
    """portfolio diagnostics: vol, Sharpe, drawdown, VaR, etc."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from portfolio.services.deflator import normalize_basis
        from portfolio.services import value_account, value_user

        account = _scope(request)
        basis = request.query_params.get("basis") or "nominal_toman"
        window, error = _int_param(request, "window", 180, allowed=(90, 180, 365))
        if error:
            return error
        try:
            normalize_basis(basis)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        weights, total, valuation = _current_weights_and_total(request.user, account)
        return Response(
            portfolio_diagnostics(
                weights,
                total,
                user=request.user,
                history_days=window,
                basis=basis,
                valuation=valuation,
            )
        )


class OptimizationView(APIView):
    """scenario optimizer: max_sharpe / min_volatility / risk_parity / hrp."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        scenario = request.data.get("scenario")
        if scenario not in SCENARIOS:
            return Response(
                {"detail": f"scenario must be one of {list(SCENARIOS)}."},
                status=400,
            )
        constraints = request.data.get("constraints")
        weights, total, _valuation = _current_weights_and_total(request.user, _scope(request))
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
                    "policy": "docs/REFERENCE.md",
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
    """efficient frontier + max_sharpe / min_volatility reference points."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .services.returns import get_universe_by_mode

        account = _scope(request)
        weights, total, _valuation = _current_weights_and_total(request.user, account)
        window, error = _int_param(request, "window", 180, clamp=(30, 3650))
        if error:
            return error
        # Scope to the user's own book. Without a universe this drew the frontier
        # over the entire active catalog while the chart caption promised "the
        # assets you already hold" -- the line and the Max-Sharpe marker described
        # a portfolio the user cannot build.
        universe = get_universe_by_mode("held", user=request.user, account=account)
        held_keys = frozenset(weights)
        try:
            frontier = _efficient_frontier(
                n_points=30,
                history_days=window,
                universe=universe,
                held_keys=held_keys,
            )
        except MixedUnitUniverseBlocked as exc:
            return Response(
                {
                    "detail": str(exc),
                    "tse_keys": exc.tse_keys,
                    "other_keys": exc.other_keys,
                    "policy": "docs/REFERENCE.md",
                },
                status=409,
            )
        # Inject the current portfolio point, on the SAME panel and the SAME
        # annualization the frontier used -- a default-window, whole-catalog
        # matrix put the user's dot on a chart built from different data.
        returns, _ = daily_returns_matrix(
            history_days=window, universe=universe, held_keys=held_keys
        )
        frequency = float(
            frontier.get("periods_per_year")
            or returns.attrs.get("periods_per_year")
            or 252
        )
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
                        ann_ret = float(port.mean() * frequency)
                        ann_vol = float(port.std(ddof=1) * np.sqrt(frequency))
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
                    means = port_returns.mean(axis=0) * frequency
                    stds = port_returns.std(axis=0, ddof=1) * np.sqrt(frequency)
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
    """"if a quant had optimized MY existing assets, what would it
    look like?" -- per lookback window, max-Sharpe and min-volatility weights
    over the user's OWN held assets, next to how the portfolio actually did.

    The math is exactly `optimize()` / `portfolio_diagnostics()`; this view is
    the window loop plus per-window error containment so a short-history user
    still sees their 1Y result even when 5Y/lifetime can't solve.
    """

    permission_classes = [IsAuthenticated]

    WINDOWS = (("1Y", 365), ("3Y", 1095), ("5Y", 1825), ("Lifetime", None))

    # ~40 `optimize()` solves per request (4 windows x up to 6 scenarios plus
    # diagnostics) is expensive enough to time out the request under a single
    # worker (see docker-compose.yml comment). Cache the whole response body
    # per (user, account, basis); the key rotates on any price update via
    # `_price_version_fingerprint()` and on any trade/holding edit via the
    # ledger/holding fingerprint below, so a cache hit can never serve a
    # result computed from stale weights or stale prices.
    CACHE_TTL = 300

    @staticmethod
    def _cache_key(user, account, basis):
        from django.core.cache import cache as _cache  # local import mirrors module style
        from .services.returns import _price_version_fingerprint

        ledger_q = LedgerEntry.objects.filter(account__user=user)
        holding_q = Holding.objects.filter(account__user=user)
        if account is not None:
            ledger_q = ledger_q.filter(account=account)
            holding_q = holding_q.filter(account=account)
        max_ledger_id = ledger_q.order_by("-id").values_list("id", flat=True).first() or 0
        max_holding_id = holding_q.order_by("-id").values_list("id", flat=True).first() or 0
        # Ticking a holding off changes the answer without inserting a row, so the
        # highest ids alone cannot see it and the cached analytics would outlive
        # the toggle for the full TTL.
        hidden_fp = "-".join(
            str(i) for i in sorted(
                holding_q.filter(is_hidden=True).values_list("id", flat=True)
            )
        )
        fingerprint = (
            f"{_price_version_fingerprint()}:{max_ledger_id}:{max_holding_id}:{hidden_fp}"
        )
        account_key = account.id if account is not None else "all"
        return f"my_optimal:{user.id}:{account_key}:{basis}:{fingerprint}", _cache

    def get(self, request):
        from .services.deflator import CpiUnavailable, normalize_basis
        from .services.returns import get_universe_by_mode

        account = _scope(request)
        weights, total, valuation = _current_weights_and_total(request.user, account)
        if not weights:
            return Response({"detail": "No priced holdings to optimize yet."}, status=400)
        universe = get_universe_by_mode("held", user=request.user, account=account)
        # These are the user's OWN assets, so they must not be screened as
        # optimizer candidates. `held_keys` relaxes the universe gates, merges
        # proxied holdings, floors the policy caps to what this book already
        # holds, and freezes anything unmeasurable at its current weight instead
        # of zeroing it (which the rebalance table rendered as a SELL).
        held_keys = frozenset(weights)
        lifetime_days = _lifetime_days(request.user, account)

        # Nominal Toman returns are dominated by rial devaluation, a factor every
        # asset shares. That single common factor inflates every pairwise
        # correlation and every expected return, so a nominal panel makes a
        # portfolio look far less diversified than it is and hands the optimizer
        # a covariance matrix that is mostly inflation. Measuring in real terms
        # strips it and shows genuine relative diversification.
        requested_basis = request.query_params.get("basis") or "real_toman"
        try:
            requested_basis = normalize_basis(requested_basis)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        cache_key, cache = self._cache_key(request.user, account, requested_basis)
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        def _solve(scenario, window_days, basis):
            """One scenario, degrading the basis rather than the answer.

            A real-terms panel needs CPI for every Jalali year it spans. When
            that is missing the request must not silently become a nominal one:
            it falls back, but the basis actually used is reported back so the
            client never mistakes an inflation-contaminated number for a real one.
            """
            try:
                return optimize(
                    scenario=scenario, current_weights=weights,
                    total_value_tomans=total, user=request.user,
                    history_days=window_days, universe=universe,
                    held_keys=held_keys, basis=basis,
                ), basis
            except CpiUnavailable:
                if basis == "nominal_toman":
                    raise
                return optimize(
                    scenario=scenario, current_weights=weights,
                    total_value_tomans=total, user=request.user,
                    history_days=window_days, universe=universe,
                    held_keys=held_keys, basis="nominal_toman",
                ), "nominal_toman"

        windows = []
        for label, fixed_days in self.WINDOWS:
            window_days = fixed_days or lifetime_days
            entry = {"label": label, "window_days": window_days}
            # min_volatility leads: it needs no return forecast, so it is the
            # scenario whose weights survive the fact that we cannot predict
            # returns. max_sharpe is kept but is no longer the only answer.
            try:
                payload, basis_used = _solve("min_volatility", window_days, requested_basis)
                entry["min_volatility"] = payload
                entry["basis"] = basis_used
            except (UniverseTooSmall, SolverError, NoAssetBeatsRiskFreeRate,
                    MixedUnitUniverseBlocked, CpiUnavailable) as exc:
                entry["status"] = "insufficient_history"
                entry["detail"] = str(exc)
                windows.append(entry)
                continue
            basis_used = entry["basis"]
            # The remaining scenarios are best-effort: one failing must not blank
            # the window, since each answers a different question about risk.
            for scenario in ("max_sharpe", "risk_parity", "hrp", "min_cvar"):
                try:
                    entry[scenario], _ = _solve(scenario, window_days, basis_used)
                except (UniverseTooSmall, SolverError, NoAssetBeatsRiskFreeRate,
                        MixedUnitUniverseBlocked, CpiUnavailable):
                    entry[scenario] = None
            # `valuation` is what supplies diagnostics' own held_keys; without it
            # the "Actual" column falls back to the strict screen and is measured
            # over a different asset set than the target it sits beside.
            entry["actual"] = portfolio_diagnostics(
                weights, total, user=request.user, history_days=window_days,
                universe=universe, valuation=valuation, basis=basis_used,
            )
            # Historical drawdown for the hypothetical scenarios: "if you had
            # held these target weights fixed for the whole window" -- the same
            # portfolio_diagnostics() computation, just fed the target weights.
            for scenario_key in ("max_sharpe", "min_volatility", "risk_parity", "hrp", "min_cvar"):
                scenario_payload = entry.get(scenario_key)
                if scenario_payload:
                    scenario_diagnostics = portfolio_diagnostics(
                        scenario_payload["target_weights"], total,
                        user=request.user, history_days=window_days,
                        universe=universe, valuation=valuation, basis=basis_used,
                    )
                    # Only the summary metrics, which is all this block was ever
                    # for. The full payload carries per-day rolling windows and a
                    # per-asset breakdown of a HYPOTHETICAL book -- 41 KB against
                    # the 625 bytes of `metrics` -- and shipping it for every
                    # scenario in every window made this response 1.6 MB and left
                    # the page blank for ten seconds. `actual` below stays whole:
                    # the risk and diversification panels genuinely read it.
                    scenario_payload["diagnostics"] = {
                        "metrics": scenario_diagnostics.get("metrics", {})
                    }
            entry["status"] = "ok"
            windows.append(entry)
        body = {"windows": windows, "basis_requested": requested_basis}
        cache.set(cache_key, body, self.CACHE_TTL)
        return Response(body)


class RobustnessView(APIView):
    """"how much of this allocation is signal?" -- bootstrap bands for ONE
    scenario and window.

    Separate from `my-optimal` because ~200 re-solves cannot run inside a page
    load that already does eight. The client requests this for the tab the user
    is actually looking at; the result is cached with the rest of the payload.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .services.returns import get_universe_by_mode

        scenario = request.query_params.get("scenario") or "min_volatility"
        if scenario not in SCENARIOS:
            return Response(
                {"detail": f"scenario must be one of {list(SCENARIOS)}."}, status=400
            )
        window, error = _int_param(request, "window", 365, clamp=(30, 3650))
        if error:
            return error

        account = _scope(request)
        weights, total, _valuation = _current_weights_and_total(request.user, account)
        if not weights:
            return Response({"detail": "No priced holdings to optimize yet."}, status=400)
        universe = get_universe_by_mode("held", user=request.user, account=account)
        try:
            payload = optimize(
                scenario=scenario,
                current_weights=weights,
                total_value_tomans=total,
                user=request.user,
                history_days=window,
                universe=universe,
                held_keys=frozenset(weights),
                include_robustness=True,
            )
        except (UniverseTooSmall, SolverError, NoAssetBeatsRiskFreeRate, MixedUnitUniverseBlocked) as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response({
            "scenario": scenario,
            "window_days": window,
            "robustness": payload.get("robustness"),
            "target_weights": payload.get("target_weights"),
            "diversification": payload.get("diversification"),
            "forecast_free": payload.get("forecast_free"),
        })


class AssetReturnsView(APIView):
    """daily-returns matrix + correlation, for heatmaps and scatter plots."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        days, _ = _int_param(request, "days", 180, clamp=(1, 365), strict=False)
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
    """"what is the best portfolio available across ALL tracked
    assets?" -- a pure read of the nightly `run_best_overall_snapshots`
    precompute (see `portfolio/tasks.py`). No solver call in
    the request path; a window with no snapshot yet reports its own status
    rather than leaving the whole response empty.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .optimization_models import OptimizationSnapshot
        from .tasks import SCENARIOS, WINDOWS_DAYS

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
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from marketdata.models import AssetMetricSnapshot

        account = _scope(request)
        if account is None:
            return Response({"detail": "account query param is required."}, status=400)
        symbols = [
            holding.asset.tse_symbol or holding.asset.brs_symbol
            for holding in account.holdings.filter(is_hidden=False).select_related("asset")
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


class DiversifierCandidatesView(APIView):
    """"What should I buy next?" ranked by diversification, not past returns.

    Scores every screened market candidate by how much portfolio volatility it
    would REMOVE if it entered the book at a small weight. Ranking by return
    picks whatever already went up; ranking by this picks what does not move
    with the book, which is the one risk reduction that costs no expected
    return. Both are returned so the frontend can plot the tradeoff rather than
    hide it.

    Advisory only: this proposes nothing and writes nothing.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from portfolio.services.deflator import CpiUnavailable, normalize_basis
        from portfolio.services.diagnostics import _portfolio_returns
        from portfolio.services.diversification import diversifier_candidates
        from portfolio.services.returns import (
            TRADING_DAYS_PER_YEAR,
            daily_returns_matrix,
            get_universe_by_mode,
        )

        account = _scope(request)
        weights, _total, valuation = _current_weights_and_total(request.user, account)
        if not weights:
            return Response({"detail": "No priced holdings to diversify yet."}, status=400)

        window, error = _int_param(request, "window", 365, allowed=(90, 180, 365))
        if error:
            return error
        try:
            basis = normalize_basis(request.query_params.get("basis") or "real_toman")
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        held = frozenset(weights)
        candidates = get_universe_by_mode("market", user=request.user, account=account) or []
        # One panel over held + candidates: the held columns build the portfolio
        # series, the rest are scored against it. `held_keys` keeps the user's
        # own holdings out of the market-universe screening gates, which would
        # otherwise delete the very columns the portfolio is made of.
        universe = sorted(held.union(candidates))
        # A real-terms panel needs CPI for every Jalali year it spans, and the
        # table is only verified through the last published figure. Degrade the
        # BASIS rather than the answer -- same rule as MyOptimalView -- and
        # report the basis actually used, so an inflation-contaminated number is
        # never mistaken for a real one.
        basis_requested = basis
        try:
            returns, _excluded = daily_returns_matrix(
                history_days=window, universe=universe, basis=basis, held_keys=held,
            )
        except CpiUnavailable:
            if basis == "nominal_toman":
                raise
            basis = "nominal_toman"
            returns, _excluded = daily_returns_matrix(
                history_days=window, universe=universe, basis=basis, held_keys=held,
            )
        if returns.empty:
            return Response({
                "basis": basis, "basis_requested": basis_requested,
                "window": window, "candidates": [], "held": [],
            })

        frequency = float(returns.attrs.get("periods_per_year", TRADING_DAYS_PER_YEAR))
        port_series = _portfolio_returns(returns, weights)
        candidate_cols = [c for c in returns.columns if c not in held]
        rows = diversifier_candidates(
            port_series,
            returns[candidate_cols],
            periods_per_year=frequency,
        )
        return Response({
            "basis": basis,
            "basis_requested": basis_requested,
            "window": window,
            "entry_weight": 0.05,
            "periods_per_year": frequency,
            # What was ACTUALLY measured, which is rarely the window asked for.
            # `_trim_to_contiguous` starts the panel after the last ingest
            # outage, so on this warehouse 90d, 180d and 365d all resolve to the
            # same ~65 rows since the 1404-1405 reopening -- three buttons that
            # cannot change the answer, with nothing on screen saying so. Every
            # correlation here carries a standard error of about 1/sqrt(n), so
            # the observation count is not a footnote: it is what decides
            # whether the ranking's top few are distinguishable at all.
            "data_window": {
                "start": returns.index[0].date().isoformat(),
                "end": returns.index[-1].date().isoformat(),
                "observations": int(len(returns.index)),
            },
            # The current book on the same axes, so the scatter can show where
            # the holdings already sit rather than plotting candidates in a void.
            "held": diversifier_candidates(
                port_series,
                returns[[c for c in returns.columns if c in held]],
                periods_per_year=frequency,
            ),
            "candidates": rows,
        })


class BenchmarkSeriesView(APIView):
    """Your portfolio against the things you could have held instead.

    Everything is indexed to 100 at the window's first shared date, because the
    question is relative growth and the levels are not comparable -- a gold gram
    and a whole portfolio have no common scale.

    The TSE index is deliberately absent: the provider exposes it as a LIVE
    snapshot only (see marketdata/endpoints.py, "there is no index history
    here"), and MarketIndexData holds ~2 weeks of rows. Plotting a benchmark
    from that would be inventing a comparison, so it is reported as unavailable
    with the reason instead.
    """

    permission_classes = [IsAuthenticated]

    # Asset keys standing in for "what else could I have held".
    BENCHMARKS = (("gold_18k_gram", "Gold (18k gram)"), ("usd_cash", "US dollar"))

    def get(self, request):
        from portfolio.services.deflator import CpiUnavailable, normalize_basis
        from portfolio.services.diagnostics import _portfolio_returns
        from portfolio.services.returns import daily_returns_matrix

        account = _scope(request)
        weights, _total, _valuation = _current_weights_and_total(request.user, account)
        if not weights:
            return Response({"detail": "No priced holdings to compare yet."}, status=400)

        window, error = _int_param(request, "window", 365, allowed=(90, 180, 365))
        if error:
            return error
        try:
            basis = normalize_basis(request.query_params.get("basis") or "nominal_toman")
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        held = frozenset(weights)
        wanted = [key for key, _label in self.BENCHMARKS]
        basis_requested = basis
        try:
            returns, _excluded = daily_returns_matrix(
                history_days=window, universe=sorted(held.union(wanted)),
                basis=basis, held_keys=held,
            )
        except CpiUnavailable:
            if basis == "nominal_toman":
                raise
            basis = "nominal_toman"
            returns, _excluded = daily_returns_matrix(
                history_days=window, universe=sorted(held.union(wanted)),
                basis=basis, held_keys=held,
            )

        port = _portfolio_returns(returns, weights)
        if port.empty:
            return Response({"basis": basis, "window": window, "series": [], "unavailable": []})

        def indexed(series):
            """Cumulative growth from 100. NaN-safe: a benchmark that starts
            later joins the chart at its own first observation rather than
            dragging the whole series to null."""
            return (100.0 * (1.0 + series.fillna(0.0)).cumprod()).round(4)

        columns = {"portfolio": indexed(port)}
        unavailable = []
        for key, label in self.BENCHMARKS:
            if key in returns.columns:
                columns[key] = indexed(returns[key].reindex(port.index))
            else:
                unavailable.append({"key": key, "label": label,
                                    "reason": "no overlapping history in this window"})
        unavailable.append({
            "key": "tse_index", "label": "TSE index",
            "reason": "provider publishes the index as a live snapshot only; no history to compare against",
        })

        rows = []
        for stamp in port.index:
            row = {"x": stamp.isoformat()}
            for name, series in columns.items():
                value = series.get(stamp)
                row[name] = None if value is None or pd.isna(value) else float(value)
            rows.append(row)

        return Response({
            "basis": basis,
            "basis_requested": basis_requested,
            "window": window,
            "series": rows,
            "labels": {"portfolio": "Your portfolio",
                       **{k: v for k, v in self.BENCHMARKS if k in columns}},
            "unavailable": unavailable,
        })


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

    permission_classes = [IsAdminUser]

    def get(self, request):
        from marketdata.models import SymbolIntegrity, RejectedRecord

        try:
            page = max(1, int(request.query_params.get("page") or 1))
            page_size = min(100, max(1, int(request.query_params.get("page_size") or 50)))
        except (TypeError, ValueError):
            return Response({"detail": "page and page_size must be integers."}, status=400)
        offset = (page - 1) * page_size

        integrities = SymbolIntegrity.objects.all().order_by("symbol")
        rejected = RejectedRecord.objects.all().order_by("-occurrences")
        integrity_data = [
            {
                "symbol": i.symbol,
                "passes_gate": i.passes_gate,
                "coverage_ratio": float(i.coverage_ratio) if i.coverage_ratio else 0.0,
                "max_gap_days": i.max_gap_days,
                "reason": i.reason,
                "computed_at": i.computed_at.isoformat() if i.computed_at else None,
            }
            for i in integrities[offset:offset + page_size]
        ]
        rejected_data = [
            {
                "id": r.id,
                "endpoint": r.endpoint,
                "symbol": r.symbol,
                "date": r.date,
                "reason": r.reason,
                "occurrences": r.occurrences,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
            for r in rejected[offset:offset + page_size]
        ]
        return Response({
            "integrity_count": integrities.count(),
            "rejected_count": rejected.count(),
            "page": page,
            "page_size": page_size,
            "integrity": integrity_data,
            "rejected": rejected_data,
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
            # This is a user-facing endpoint: never infer cross-account access
            # from staff status. Staff-only operational reads belong on an
            # explicit admin endpoint with its own permission contract.
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
        account_id = request.query_params.get("account_id")
        if account_id:
            account = get_object_or_404(Account, pk=account_id, user=request.user)
            snap = OptimizationSnapshot.objects.filter(account=account).order_by("-created_at").first()
        else:
            # latest global snapshot
            snap = OptimizationSnapshot.objects.filter(account__isnull=True).order_by("-created_at").first()

        if not snap:
            return Response({"detail": "Not found."}, status=404)
        serializer = OptimizationSnapshotSerializer(snap)
        return Response(serializer.data)


class ComparisonView(APIView):
    """Counterfactuals: what the same money would have done somewhere else.

    `GET` with no `mode` answers what the picker can offer; with one, it runs
    that comparison. Every refusal comes back as a `reason` code the page
    renders as a sentence, because most of them are data limits the user can
    act on -- property has no market series, a position with no recorded prices
    has no amount to move, a coin's history may not reach back to 2022.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from portfolio.services.comparison import (
            MAX_WINDOW_DAYS, ComparisonError, comparable_assets, compare,
        )

        account = _scope(request)
        mode = request.query_params.get("mode")
        if not mode:
            return Response(comparable_assets(request.user, account))
        days, error = _int_param(request, "days", 0, clamp=(0, MAX_WINDOW_DAYS))
        if error:
            return error
        try:
            return Response(compare(
                request.user,
                account=account,
                mode=mode,
                subject=request.query_params.get("subject"),
                target=request.query_params.get("target"),
                # 0 is "as far back as my own history goes", which is the
                # answer this page is usually asked for.
                days=days or None,
            ))
        except ComparisonError as exc:
            return Response(
                {"detail": exc.detail, "reason": exc.reason, **exc.extra}, status=400
            )

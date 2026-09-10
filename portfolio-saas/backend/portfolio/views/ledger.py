"""The ledger: dated events, trades, imports and their reversals.

Every write here goes through `services.ledger`, which owns the replay
and the invariants; these views are the HTTP surface over it."""
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from ..models import Holding, LedgerEntry, Transaction
from ..serializers import (
    LedgerEntryInputSerializer,
    LedgerEntryPatchSerializer,
    LedgerEntrySerializer,
    TradeInputSerializer,
    TransactionSerializer,
)
from ..services import execute_trade, get_latest_prices, undo_trade
from ..services.catalog import resolve_asset_key
from ..services.trades import TradeError
from ..services.ledger import (
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
from ..services.imports import (
    LedgerImportError,
    commit_ledger_import,
    preview_ledger_import,
)
from ._common import _int_param


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
            asset = resolve_asset_key(request.user, data["asset_key"])
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
                    cost_basis_tomans=data.get("cost_basis_tomans"),
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
                cost_basis_tomans=data.get("cost_basis_tomans"),
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
        # Context, so `validate` can scope asset_key to this user. The Liability
        # and Holding serializers get it for free from DRF's generic views; this
        # one is constructed by hand and therefore has to be handed it.
        form = TradeInputSerializer(data=request.data, context={"request": request})
        form.is_valid(raise_exception=True)
        data = form.validated_data
        asset = resolve_asset_key(request.user, data["asset_key"])
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
    permission_classes = [IsAuthenticated]

    def post(self, request, tx_id):
        try:
            undo_trade(user=request.user, transaction_id=tx_id)
        except Transaction.DoesNotExist:
            return Response({"detail": "Not found."}, status=404)
        except TradeError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"detail": "Transaction undone successfully."})


class TransactionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        days, _ = _int_param(request, "days", 90, clamp=(1, 3650), strict=False)
        rows = Transaction.objects.filter(
            account__user=request.user,
            timestamp__gte=timezone.now() - timedelta(days=days),
            reversal_of__isnull=True,
            reversed_by__isnull=True,
        ).select_related("asset")
        account_id = request.query_params.get("account")
        if account_id:
            try:
                rows = rows.filter(account_id=int(account_id))
            except (TypeError, ValueError):
                return Response({"detail": "account must be an integer id."}, status=400)
        return Response(TransactionSerializer(rows, many=True).data)


class TransactionDestroyView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            undo_trade(user=request.user, transaction_id=pk)
        except Transaction.DoesNotExist:
            return Response({"detail": "Transaction not found."}, status=404)
        except TradeError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"detail": "Transaction undone successfully."})

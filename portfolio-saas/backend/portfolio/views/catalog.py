"""The asset catalog and the CRUD around what a user owns.

Assets, accounts, holdings and liabilities: the endpoints that change
the shape of a portfolio rather than reporting on it."""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from ..models import Asset, Holding, LedgerEntry, Liability
from ..serializers import (
    AccountSerializer,
    AssetSerializer,
    HoldingSerializer,
    LiabilitySerializer,
)
from ..services.ledger import (
    LedgerError,
    create_ledger_entry,
    delete_orphan_holding,
    set_orphan_holding,
)
from ..services.catalog import ensure_asset, search_catalog


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
        # HoldingSerializer reads five columns off `asset` plus `obj.label`, so
        # without the join this is one extra query per row on the most-hit
        # endpoint in the app.
        if account is None:
            return Holding.objects.none()
        return account.holdings.select_related("asset").all()

    def _account(self):
        return (
            self.request.user.accounts.filter(pk=self.kwargs["account_id"]).first()
        )

    def perform_create(self, serializer):
        from ..services.ledger import (
            backfill_house_into_snapshots,
            record_house_mark,
            record_manual_price,
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
                    cost_basis_tomans=data.get("purchase_price_per_sqm_million"),
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
        ).select_related("asset")

    def perform_update(self, serializer):
        from ..services.ledger import (
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
                                       "mortgage_deduction_tomans",
                                       "purchase_price_per_sqm_million")):
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
            if (
                data.get("quantity") == 0
                and LedgerEntry.objects.filter(
                    account=serializer.instance.account, asset=asset
                ).exists()
                and not data.get("confirm_sell_all", False)
            ):
                raise ValidationError({
                    "confirm_sell_all": [
                        "Confirm selling all units before removing this holding."
                    ]
                })
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
                # Not carried forward, and deliberately: marks REPLACE, but
                # `performance._house_position` reads the latest mark that
                # DECLARED a basis, so a plain revaluation leaves the purchase
                # price standing instead of restating it as today's figure.
                cost_basis_tomans=data.get("purchase_price_per_sqm_million"),
                occurred_at=data.get("occurred_at"),
            )
        except LedgerError as exc:
            raise ValidationError(str(exc)) from exc
        serializer.instance = Holding.objects.get(
            account_id=self.kwargs["account_id"],
            asset_id=serializer.instance.asset_id,
        )

    def perform_destroy(self, instance):
        from ..services.ledger import (
            adjust_holding_quantity,
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


class LiabilityListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LiabilitySerializer

    def get_queryset(self):
        # LiabilitySerializer renders `asset_key`/`asset_name`, so the join has
        # to be here or every secured debt costs its own query.
        return Liability.objects.filter(
            account__user=self.request.user,
            account_id=self.kwargs["account_id"],
        ).select_related("asset")

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
        ).select_related("asset")

from decimal import Decimal
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Account,
    Asset,
    Holding,
    LedgerEntry,
    Transaction,
    Liability,
)
from .services.ledger import (
    PriceResolutionError,
    assert_not_before_history,
    resolve_historical_price,
)


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        # `tse_symbol` is exposed because it is the unit discriminator, not just
        # a join key: a non-empty one means this asset's prices are quoted in
        # Rial. The add-transaction dialog labels its price field from it, and
        # must use the same test the server does (currency.is_tse_priced).
        fields = ("id", "key", "name", "name_fa", "asset_class", "currency",
                  "is_manual", "is_house", "is_active", "tse_symbol")


class HoldingSerializer(serializers.ModelSerializer):
    # `asset_key` is optional so a brand-new property can be created in the same
    # POST that opens the holding -- there is no catalog row to point at yet.
    quantity = serializers.DecimalField(
        max_digits=20,
        decimal_places=6,
        min_value=Decimal("0"),
        required=False,
    )
    asset_key = serializers.SlugRelatedField(
        source="asset", slug_field="key", queryset=Asset.objects.none(), required=False
    )
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    asset_name_fa = serializers.CharField(source="asset.name_fa", read_only=True)
    asset_class = serializers.CharField(source="asset.asset_class", read_only=True)
    is_house = serializers.BooleanField(source="asset.is_house", read_only=True)
    is_manual = serializers.BooleanField(source="asset.is_manual", read_only=True)
    unit_price_tomans = serializers.DecimalField(
        max_digits=20,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=False,
        write_only=True,
    )
    # A property is described the way its owner describes it: how big, and what a
    # square meter is worth. `quantity` stores the second of those in millions of
    # Toman (see HOUSE_AREA_SQM / valuation._house_value), which is meaningless on
    # screen, so it is never the field the client reads or writes for a house.
    price_per_sqm_million = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001"),
        required=False, write_only=True,
    )
    price_per_sqm_tomans = serializers.SerializerMethodField()
    gross_value_tomans = serializers.SerializerMethodField()
    label = serializers.SerializerMethodField()
    # Only supplied when minting a new property; ignored otherwise.
    new_property_name = serializers.CharField(
        max_length=120, required=False, write_only=True
    )
    # Lets a client date a revaluation it is entering after the fact. Declared
    # here so DRF parses it: the view used to read it straight off request.data
    # and hand the raw STRING to a datetime comparison, so dating a property
    # mark crashed with a TypeError instead of being accepted or refused.
    occurred_at = serializers.DateTimeField(required=False, write_only=True)

    class Meta:
        model = Holding
        fields = ("id", "asset_key", "asset_name", "asset_name_fa", "asset_class", "is_house", "is_manual",
                  "quantity", "unit_price_tomans", "area_sqm", "mortgage_deduction_tomans",
                  "display_name", "label", "is_hidden",
                  "price_per_sqm_million", "price_per_sqm_tomans", "gross_value_tomans",
                  "new_property_name", "occurred_at",
                  "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scoped per request so one user cannot attach another user's property to
        # their own account by guessing its key.
        user = getattr(self.context.get("request"), "user", None)
        catalog = Asset.objects.filter(is_active=True)
        self.fields["asset_key"].queryset = (
            catalog.filter(Q(owner__isnull=True) | Q(owner=user))
            if user is not None and user.is_authenticated
            else catalog.filter(owner__isnull=True)
        )

    def get_label(self, obj) -> str:
        return obj.label

    def get_price_per_sqm_tomans(self, obj):
        price = obj.price_per_sqm_tomans
        return None if price is None else str(price)

    def get_gross_value_tomans(self, obj):
        price = obj.price_per_sqm_tomans
        return None if price is None else str(price * Decimal(obj.area_sqm))

    def validate_quantity(self, value):
        if value == 0 and self.context.get("request") and self.context["request"].method == "POST":
            raise serializers.ValidationError("Quantity must be positive.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        creating = request is not None and request.method == "POST"
        # A house's price-per-sqm and the generic `quantity` are the same column;
        # accept either name and normalise here so no downstream branch has to.
        if "price_per_sqm_million" in attrs:
            attrs["quantity"] = attrs.pop("price_per_sqm_million")
        if creating:
            if not attrs.get("asset") and not attrs.get("new_property_name"):
                raise serializers.ValidationError(
                    {"asset_key": "Choose an asset, or name a new property."}
                )
            if attrs.get("quantity") is None:
                raise serializers.ValidationError({"quantity": "This field is required."})
            if attrs.get("new_property_name") and not attrs.get("area_sqm"):
                raise serializers.ValidationError(
                    {"area_sqm": "A property needs its size in square meters."}
                )
        return attrs


class AccountSerializer(serializers.ModelSerializer):
    holdings = HoldingSerializer(many=True, read_only=True)

    class Meta:
        model = Account
        fields = (
            "id", "name", "broker", "goal", "holdings", "tracking_started_at",
            "cash_balance_tomans", "ledger_complete", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "tracking_started_at", "cash_balance_tomans", "ledger_complete",
            "created_at", "updated_at",
        )


class LedgerEntryInputSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=LedgerEntry.Kind.choices)
    asset_key = serializers.SlugField(required=False)
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001"), required=False
    )
    unit_price_tomans = serializers.DecimalField(
        max_digits=20, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    amount_tomans = serializers.DecimalField(
        max_digits=24, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    area_sqm = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    mortgage_deduction_tomans = serializers.DecimalField(
        max_digits=20, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    occurred_at = serializers.DateTimeField(required=False, default=timezone.now)
    source = serializers.ChoiceField(choices=("manual", "csv"), default="manual")
    note = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    external_id = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")

    def validate_occurred_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Cannot be in the future.")
        return value


class LedgerEntrySerializer(serializers.ModelSerializer):
    asset_key = serializers.CharField(source="asset.key", allow_null=True, read_only=True)
    asset_name = serializers.CharField(source="asset.name", allow_null=True, read_only=True)
    asset_name_fa = serializers.CharField(source="asset.name_fa", allow_null=True, read_only=True)
    occurred_at = serializers.DateTimeField(source="timestamp", read_only=True)
    unit_price_tomans = serializers.DecimalField(
        source="price_tomans", max_digits=20, decimal_places=4, allow_null=True, read_only=True
    )
    pnl_tomans = serializers.SerializerMethodField()
    pnl_kind = serializers.SerializerMethodField()
    account_id = serializers.IntegerField(source="account.id", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)
    is_synthetic = serializers.SerializerMethodField()
    label = serializers.SerializerMethodField()
    # A property's `quantity` is a price per square meter, not a count, so the
    # client has to know which convention to render before it prints the number.
    is_house = serializers.BooleanField(source="asset.is_house", read_only=True, default=False)

    class Meta:
        model = LedgerEntry
        fields = (
            "id", "kind", "asset_key", "asset_name", "asset_name_fa", "label",
            "is_house", "quantity", "unit_price_tomans",
            "amount_tomans", "area_sqm", "mortgage_deduction_tomans",
            "occurred_at", "source", "note", "external_id", "reversal_of",
            "created_at", "pnl_tomans", "pnl_kind",
            "account_id", "account_name", "is_synthetic",
        )
        read_only_fields = fields

    def _pnl(self, obj):
        return (self.context.get("pnl") or {}).get(obj.pk) or {}

    def get_pnl_tomans(self, obj):
        return self._pnl(obj).get("pnl_tomans")

    def get_pnl_kind(self, obj):
        return self._pnl(obj).get("pnl_kind")

    def get_is_synthetic(self, obj):
        return False

    def get_label(self, obj) -> str | None:
        """The owner's name for the asset this entry moved.

        Supplied by the view as a {(account_id, asset_id): name} map rather than
        looked up per row -- the alternative is one query per ledger line. Falls
        back to the catalog name when the holding is gone (a fully sold position
        keeps its history)."""
        if not obj.asset_id:
            return None
        names = self.context.get("labels") or {}
        return (
            names.get((obj.account_id, obj.asset_id))
            or obj.asset.name_fa
            or obj.asset.name
        )


class LedgerEntryPatchSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001"), required=False
    )
    unit_price_tomans = serializers.DecimalField(
        max_digits=20, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    amount_tomans = serializers.DecimalField(
        max_digits=24, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    # A property's size is part of the row the ledger shows, so it has to be part
    # of the row the ledger can correct. Without it the endpoint accepted the
    # field, dropped it, and answered 200 -- a resize that looked saved and was not.
    area_sqm = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    occurred_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_occurred_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Cannot be in the future.")
        return value


class TradeInputSerializer(serializers.Serializer):
    """Validates a buy/sell request. `asset_key` resolves to an Asset in the view."""

    asset_key = serializers.SlugField()
    side = serializers.ChoiceField(choices=Transaction.Side.choices)
    quantity = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=Decimal("0.000001"))
    note = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    timestamp = serializers.DateTimeField(required=False)
    price_tomans = serializers.DecimalField(max_digits=20, decimal_places=4, required=False, allow_null=True)
    source = serializers.ChoiceField(choices=(("manual", "manual"), ("imported", "imported"), ("inferred", "inferred")), default="manual")

    def validate(self, attrs):
        asset_key = attrs.get('asset_key')
        timestamp = attrs.get('timestamp') or timezone.now()
        price_tomans = attrs.get('price_tomans')
        
        if timestamp > timezone.now():
            raise serializers.ValidationError({"timestamp": "Transaction timestamp cannot be in the future."})

        # Get the asset
        try:
            asset = Asset.objects.get(key=asset_key, is_active=True)
        except Asset.DoesNotExist:
            raise serializers.ValidationError({"asset_key": "Invalid or inactive asset."})

        try:
            assert_not_before_history(asset, timestamp)
            if not price_tomans:
                attrs["price_tomans"] = resolve_historical_price(asset, timestamp)
        except PriceResolutionError as exc:
            raise serializers.ValidationError({exc.field: str(exc)}) from exc

        attrs["timestamp"] = timestamp
        return attrs


class TransactionSerializer(serializers.ModelSerializer):
    """Read view of a ledger row for the trade history / chart markers."""

    asset_key = serializers.CharField(source="asset.key", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    is_latest_for_asset = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = ("id", "asset_key", "asset_name", "side", "quantity",
                  "price_tomans", "note", "timestamp", "is_latest_for_asset")

    def get_is_latest_for_asset(self, obj) -> bool:
        latest_id = (
            Transaction.objects.filter(
                account=obj.account, asset=obj.asset,
                reversal_of__isnull=True, reversed_by__isnull=True
            )
            .order_by("-timestamp", "-pk")
            .values_list("pk", flat=True)
            .first()
        )
        return latest_id == obj.pk


class LiabilitySerializer(serializers.ModelSerializer):
    asset_key = serializers.SlugRelatedField(
        source="asset",
        slug_field="key",
        queryset=Asset.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    asset_name = serializers.CharField(source="asset.name", read_only=True)

    class Meta:
        model = Liability
        fields = (
            "id",
            "account",
            "label",
            "amount_tomans",
            "asset_key",
            "asset_name",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "account", "created_at", "updated_at")


# Serializer for OptimizationSnapshot persisted records
class OptimizationSnapshotSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    # source=account_id: the field carries the FK object otherwise, and
    # IntegerField(Account) raises a 500 on every account-scoped snapshot.
    account = serializers.IntegerField(source="account_id", allow_null=True, read_only=True)
    scenario = serializers.CharField(read_only=True)
    payload = serializers.JSONField(read_only=True)
    price_version = serializers.CharField(read_only=True)
    as_of = serializers.DateTimeField(allow_null=True, read_only=True)
    created_at = serializers.DateTimeField(read_only=True)

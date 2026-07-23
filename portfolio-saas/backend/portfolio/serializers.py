from decimal import Decimal

from rest_framework import serializers

from .models import Account, Asset, Holding, Transaction


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = ("id", "key", "name", "name_fa", "asset_class", "currency",
                  "is_manual", "is_house", "is_active")


class HoldingSerializer(serializers.ModelSerializer):
    quantity = serializers.DecimalField(
        max_digits=20,
        decimal_places=6,
        min_value=Decimal("0.000001"),
    )
    asset_key = serializers.SlugRelatedField(
        source="asset", slug_field="key", queryset=Asset.objects.filter(is_active=True)
    )
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    asset_name_fa = serializers.CharField(source="asset.name_fa", read_only=True)
    asset_class = serializers.CharField(source="asset.asset_class", read_only=True)
    is_house = serializers.BooleanField(source="asset.is_house", read_only=True)

    class Meta:
        model = Holding
        fields = ("id", "asset_key", "asset_name", "asset_name_fa", "asset_class", "is_house",
                  "quantity", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")


class AccountSerializer(serializers.ModelSerializer):
    holdings = HoldingSerializer(many=True, read_only=True)

    class Meta:
        model = Account
        fields = ("id", "name", "broker", "goal", "holdings", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")


class TradeInputSerializer(serializers.Serializer):
    """Validates a buy/sell request. `asset_key` resolves to an Asset in the view."""

    asset_key = serializers.SlugField()
    side = serializers.ChoiceField(choices=Transaction.Side.choices)
    quantity = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=Decimal("0.000001"))
    note = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")


class TransactionSerializer(serializers.ModelSerializer):
    """Read view of a ledger row for the trade history / chart markers."""

    asset_key = serializers.CharField(source="asset.key", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)

    class Meta:
        model = Transaction
        fields = ("id", "asset_key", "asset_name", "side", "quantity",
                  "price_tomans", "note", "timestamp")

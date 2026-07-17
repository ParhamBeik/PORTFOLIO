from rest_framework import serializers

from .models import Account, Asset, Holding


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = ("id", "key", "name", "name_fa", "asset_class", "currency",
                  "is_manual", "is_house", "is_active")


class HoldingSerializer(serializers.ModelSerializer):
    asset_key = serializers.SlugRelatedField(
        source="asset", slug_field="key", queryset=Asset.objects.all()
    )
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    asset_class = serializers.CharField(source="asset.asset_class", read_only=True)

    class Meta:
        model = Holding
        fields = ("id", "asset_key", "asset_name", "asset_class",
                  "quantity", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")


class AccountSerializer(serializers.ModelSerializer):
    holdings = HoldingSerializer(many=True, read_only=True)

    class Meta:
        model = Account
        fields = ("id", "name", "broker", "holdings", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

from decimal import Decimal
from django.utils import timezone
from rest_framework import serializers

from .models import Account, Asset, Holding, LedgerEntry, Transaction, BacktestRun, BacktestYear, Watchlist, WatchlistItem
from marketdata.models import MarketCandle, GoldCurrencyHistory
from marketdata.jalali import normalize_jalali
import jdatetime


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
    occurred_at = serializers.DateTimeField(source="timestamp", read_only=True)
    unit_price_tomans = serializers.DecimalField(
        source="price_tomans", max_digits=20, decimal_places=4, allow_null=True, read_only=True
    )

    class Meta:
        model = LedgerEntry
        fields = (
            "id", "kind", "asset_key", "quantity", "unit_price_tomans",
            "amount_tomans", "occurred_at", "source", "note", "external_id",
            "reversal_of", "created_at",
        )
        read_only_fields = fields


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
        
        # If timestamp is provided, we need to check bounds and possibly resolve price
        # Get Jalali date for the timestamp
        dt_date = timestamp.date()
        try:
            j_date_str = jdatetime.date.fromgregorian(date=dt_date).strftime("%Y-%m-%d")
        except Exception:
            raise serializers.ValidationError({"timestamp": "Could not convert to Jalali date."})
            
        if not asset.is_manual and not asset.is_house:
            # Check earliest available price
            if asset.asset_class == Asset.AssetClass.STOCK and asset.tse_symbol:
                first_record = MarketCandle.objects.filter(symbol=asset.tse_symbol, timeframe="1d_unadj").order_by("date_time").first()
                if first_record and j_date_str < first_record.date_time.split(" ")[0]:
                    raise serializers.ValidationError({"timestamp": f"Date is before the earliest available price date ({first_record.date_time})."})
            elif asset.asset_class in (Asset.AssetClass.GOLD, Asset.AssetClass.CASH, Asset.AssetClass.CRYPTO) and asset.brs_symbol:
                first_record = GoldCurrencyHistory.objects.filter(symbol=asset.brs_symbol).order_by("date").first()
                if first_record and j_date_str < first_record.date:
                    raise serializers.ValidationError({"timestamp": f"Date is before the earliest available price date ({first_record.date})."})
            
            # Resolve price if omitted or 0
            if not price_tomans:
                if asset.asset_class == Asset.AssetClass.STOCK and asset.tse_symbol:
                    candle = MarketCandle.objects.filter(symbol=asset.tse_symbol, timeframe="1d_unadj", date_time__startswith=j_date_str).first()
                    if candle:
                        attrs['price_tomans'] = candle.close_price
                    else:
                        raise serializers.ValidationError({"price_tomans": "Price omitted and no historical price found for this date."})
                elif asset.asset_class in (Asset.AssetClass.GOLD, Asset.AssetClass.CASH, Asset.AssetClass.CRYPTO) and asset.brs_symbol:
                    history = GoldCurrencyHistory.objects.filter(symbol=asset.brs_symbol, date=j_date_str).first()
                    if history:
                        attrs['price_tomans'] = history.close_price
                    else:
                        raise serializers.ValidationError({"price_tomans": "Price omitted and no historical price found for this date."})

        # Make sure timestamp is in attrs
        attrs['timestamp'] = timestamp

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
            Transaction.objects.filter(account=obj.account, asset=obj.asset)
            .order_by("-timestamp", "-pk")
            .values_list("pk", flat=True)
            .first()
        )
        return latest_id == obj.pk


class BacktestYearSerializer(serializers.ModelSerializer):
    class Meta:
        model = BacktestYear
        fields = (
            "id", "cutoff_date", "scenario", "target_weights",
            "realized_metrics", "benchmark_delta", "excluded_symbols",
        )


class BacktestRunSerializer(serializers.ModelSerializer):
    years = BacktestYearSerializer(many=True, read_only=True)

    class Meta:
        model = BacktestRun
        fields = (
            "id", "account", "basis", "universe_mode", "universe",
            "completed_years", "status", "progress", "error", "manifest",
            "integrity_version", "created_at", "completed_at", "years",
        )


class BacktestRequestSerializer(serializers.Serializer):
    account_id = serializers.IntegerField(min_value=1)
    universe_mode = serializers.ChoiceField(
        choices=("portfolio", "verified_market")
    )
    symbols = serializers.ListField(
        child=serializers.CharField(max_length=120),
        required=False,
        default=list,
        max_length=100,
    )
    basis = serializers.ChoiceField(
        choices=("nominal_toman", "usd_denominated", "nominal", "usd_real")
    )
    completed_years = serializers.IntegerField(min_value=1, max_value=5, default=5)

    def validate_symbols(self, value):
        symbols = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(symbols) != len(value):
            raise serializers.ValidationError("symbols must be unique and non-empty.")
        return symbols


class WatchlistItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = WatchlistItem
        fields = ("id", "symbol", "force_include", "force_exclude", "created_at")


class WatchlistSerializer(serializers.ModelSerializer):
    items = WatchlistItemSerializer(many=True, read_only=True)

    class Meta:
        model = Watchlist
        fields = ("id", "account", "items", "created_at")

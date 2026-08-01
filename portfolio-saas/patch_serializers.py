import re

file_path = "backend/portfolio/serializers.py"
with open(file_path, "r") as f:
    content = f.read()

imports_replacement = """from decimal import Decimal
from django.utils import timezone
from rest_framework import serializers

from .models import Account, Asset, Holding, Transaction
from marketdata.models import MarketCandle, GoldCurrencyHistory
from marketdata.jalali import normalize_jalali
import jdatetime"""

content = re.sub(r'from decimal import Decimal\n\nfrom rest_framework import serializers\n\nfrom \.models import Account, Asset, Holding, Transaction', imports_replacement, content)


trade_input_replacement = """class TradeInputSerializer(serializers.Serializer):
    \"\"\"Validates a buy/sell request. `asset_key` resolves to an Asset in the view.\"\"\"

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

        if not attrs.get('price_tomans'):
             attrs['price_tomans'] = Decimal("0")
             
        # Make sure timestamp is in attrs
        attrs['timestamp'] = timestamp

        return attrs"""

content = re.sub(r'class TradeInputSerializer\(serializers\.Serializer\):[\s\S]*?(?=class TransactionSerializer)', trade_input_replacement + "\n\n\n", content)

with open(file_path, "w") as f:
    f.write(content)

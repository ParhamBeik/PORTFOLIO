import os
import django

# Set up Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from marketdata.models import DailyStockHistory, GoldCurrencyHistory, MarketInstrument, ArchiveFetchState
from portfolio.models import Asset

print("=== Eligible Gold/Currency MarketInstruments in DB ===")
for mi in MarketInstrument.objects.filter(source=MarketInstrument.Source.BRS):
    print(f"Symbol: {mi.symbol}, Name: {mi.name}, Category: {mi.category}, Group: {mi.provider_group}, Eligible: {mi.eligible}")

print("\n=== Active Assets in DB ===")
for asset in Asset.objects.all():
    print(f"Key: {asset.key}, Name: {asset.name}, BRS Symbol: {asset.brs_symbol}, TSE Symbol: {asset.tse_symbol}, Class: {asset.asset_class}")

print("\n=== GoldCurrencyHistory row counts per symbol ===")
from django.db.models import Count
for row in GoldCurrencyHistory.objects.values("symbol").annotate(count=Count("id")):
    print(f"Symbol: {row['symbol']}, Count: {row['count']}")

print("\n=== USDT data inspection ===")
usdt_rows = GoldCurrencyHistory.objects.filter(symbol__icontains="USDT")
print(f"Found {usdt_rows.count()} rows for symbol matching USDT")
for row in usdt_rows[:10]:
    print(f"Symbol: {row.symbol}, Date: {row.date}, Close: {row.close_price}")

print("\n=== USD data inspection ===")
usd_rows = GoldCurrencyHistory.objects.filter(symbol="USD").order_by("date")
print(f"Found {usd_rows.count()} rows for USD")
for row in usd_rows[:10]:
    print(f"Symbol: {row.symbol}, Date: {row.date}, Close: {row.close_price}")
for row in usd_rows[usd_rows.count()-10:]:
    print(f"Symbol: {row.symbol}, Date: {row.date}, Close: {row.close_price}")

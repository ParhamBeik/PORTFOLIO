import os
import sys
import django

# Setup django
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.db.models import Count, Q, Sum
from marketdata.models import (
    DailyStockHistory, MarketCandle, StockTransactionTick,
    GoldCurrencyHistory, RejectedRecord, SystemLogEvent
)
import re

JALALI_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

print("============================================================")
print("DEEP DATA QUALITY AUDIT REPORT")
print("============================================================")

# 1. DailyStockHistory Quality
print("\n[1] DailyStockHistory Quality Metrics:")
total_dsh = DailyStockHistory.objects.count()
print(f"  - Total Rows: {total_dsh:,}")
if total_dsh > 0:
    empty_symbol = DailyStockHistory.objects.filter(symbol="").count()
    zero_close = DailyStockHistory.objects.filter(pc=0).count()
    zero_volume = DailyStockHistory.objects.filter(tvol=0).count()
    adjusted_rows = DailyStockHistory.objects.filter(is_adjusted=True).count()
    unadjusted_rows = DailyStockHistory.objects.filter(is_adjusted=False).count()
    
    # Check date format format samples
    sample_dates = DailyStockHistory.objects.values_list('date', flat=True)[:1000]
    invalid_date_format_count = sum(1 for d in sample_dates if not JALALI_DATE_RE.match(d))
    
    print(f"  - Empty Symbol Count: {empty_symbol} ({empty_symbol/total_dsh*100:.3f}%)")
    print(f"  - Zero Close Price Count: {zero_close} ({zero_close/total_dsh*100:.3f}%)")
    print(f"  - Zero Volume (Suspended Days) Count: {zero_volume} ({zero_volume/total_dsh*100:.3f}%)")
    print(f"  - Adjusted Price Rows: {adjusted_rows:,} ({adjusted_rows/total_dsh*100:.1f}%)")
    print(f"  - Unadjusted Price Rows: {unadjusted_rows:,} ({unadjusted_rows/total_dsh*100:.1f}%)")
    print(f"  - Date format issues in sample of 1000: {invalid_date_format_count}")

# 2. MarketCandle Quality
print("\n[2] MarketCandle Quality Metrics:")
total_mc = MarketCandle.objects.count()
print(f"  - Total Rows: {total_mc:,}")
if total_mc > 0:
    empty_symbol_mc = MarketCandle.objects.filter(symbol="").count()
    zero_close_mc = MarketCandle.objects.filter(close_price=0).count()
    zero_volume_mc = MarketCandle.objects.filter(volume=0).count()
    
    timeframes = MarketCandle.objects.values('timeframe').annotate(count=Count('id'))
    
    print(f"  - Empty Symbol Count: {empty_symbol_mc}")
    print(f"  - Zero Close Price Count: {zero_close_mc}")
    print(f"  - Zero Volume Count: {zero_volume_mc}")
    print("  - Timeframe Breakdown:")
    for tf in timeframes:
        print(f"    * {tf['timeframe']}: {tf['count']:,} rows")

# 3. StockTransactionTick Quality
print("\n[3] StockTransactionTick Quality Metrics:")
total_stt = StockTransactionTick.objects.count()
print(f"  - Total Rows: {total_stt:,}")
if total_stt > 0:
    canceled_ticks = StockTransactionTick.objects.filter(canceled=True).count()
    active_ticks = StockTransactionTick.objects.filter(canceled=False).count()
    zero_price_ticks = StockTransactionTick.objects.filter(price=0).count()
    zero_vol_ticks = StockTransactionTick.objects.filter(volume=0).count()
    
    print(f"  - Canceled Transactions: {canceled_ticks:,} ({canceled_ticks/total_stt*100:.2f}%)")
    print(f"  - Active Transactions: {active_ticks:,} ({active_ticks/total_stt*100:.2f}%)")
    print(f"  - Zero Price Transactions: {zero_price_ticks}")
    print(f"  - Zero Volume Transactions: {zero_vol_ticks}")

# 4. GoldCurrencyHistory Quality
print("\n[4] GoldCurrencyHistory Quality Metrics:")
total_gch = GoldCurrencyHistory.objects.count()
print(f"  - Total Rows: {total_gch:,}")
if total_gch > 0:
    zero_close_gch = GoldCurrencyHistory.objects.filter(close_price=0).count()
    symbols = GoldCurrencyHistory.objects.values('symbol', 'name').annotate(count=Count('id')).order_by('-count')[:5]
    print(f"  - Zero Close Price Count: {zero_close_gch}")
    print("  - Top 5 Gold/Currency Symbols by rows:")
    for s in symbols:
         print(f"    * {s['symbol']} ({s['name']}): {s['count']:,} rows")

# 5. RejectedRecord Breakdown
print("\n[5] RejectedRecord Detailed Breakdown (Format/Quality Rejections):")
rejections = RejectedRecord.objects.values('endpoint', 'reason').annotate(occurrences_sum=Sum('occurrences'), count=Count('id')).order_by('-occurrences_sum')
for r in rejections:
    print(f"  - Endpoint: {r['endpoint']:<30} | Reason: {r['reason']:<30} | Records: {r['count']:<5} | Occurrences: {r['occurrences_sum']:,}")

print("\n============================================================")

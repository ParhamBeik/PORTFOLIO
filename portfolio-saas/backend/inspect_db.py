import os
import sys
import django

# Setup django
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.apps import apps
from django.db.models import Count, Max, Min, Sum
from accounts.models import User
from portfolio.models import Asset, Account, Holding, Price, Transaction, Snapshot
from marketdata.models import (
    MarketInstrument, DailyStockHistory, MarketCandle,
    StockTransactionTick, GoldCurrencyHistory, SystemLogEvent, RejectedRecord
)

print("=" * 60)
print("DATABASE STATE AUDIT")
print("=" * 60)

# 1. Total table counts
print("\n[1] Row Counts across all models:")
for model in apps.get_models():
    count = model.objects.count()
    print(f"  - {model.__name__}: {count}")

# 2. Users
print("\n[2] Users in Database:")
for user in User.objects.all():
    print(f"  - Email: {user.email}")
    print(f"    Tier: {user.tier}, Is Pro: {user.is_pro()}, Expiry: {user.pro_expires_at}")

# 3. Portfolios and Holdings
print("\n[3] Portfolios / Accounts & Holdings:")
# Get latest price for each asset
latest_prices = {}
for asset in Asset.objects.all():
    latest_price = Price.objects.filter(asset=asset).order_by('-fetched_at').first()
    latest_prices[asset.key] = latest_price.price if latest_price else 0

for account in Account.objects.all():
    print(f"  - Account: {account.name} (Owner: {account.user.email})")
    print(f"    Broker: {account.broker or 'None'}, Goal: {account.goal or 'None'}")
    holdings = account.holdings.all()
    if not holdings.exists():
        print("    (No holdings)")
    else:
        print("    Holdings:")
        for holding in holdings:
            asset = holding.asset
            qty = holding.quantity
            lp = latest_prices.get(asset.key, 0)
            val = float(qty) * float(lp)
            print(f"      * {asset.name} ({asset.key}): Qty={qty}, Unit Price={lp} Tomans, Value={val:,.2f} Tomans")

# 4. Transactions
print("\n[4] Recent Transactions (last 5):")
txns = Transaction.objects.order_by('-timestamp')[:5]
if not txns.exists():
    print("  No transactions found.")
else:
    for txn in txns:
        print(f"  - [{txn.timestamp}] Account: {txn.account.name} | Asset: {txn.asset.key} | Side: {txn.side} | Qty: {txn.quantity} | Unit Price: {txn.price_tomans} Tomans")

# 5. Snapshots
print("\n[5] Net Worth Snapshots Summary:")
snapshots = Snapshot.objects.all()
if not snapshots.exists():
    print("  No snapshots found.")
else:
    print(f"  Total Snapshots: {snapshots.count()}")
    latest_snap = snapshots.order_by('-timestamp').first()
    print(f"  Latest Snapshot: Time={latest_snap.timestamp}, Total Value={latest_snap.total_value_tomans:,.2f} Tomans, Estimated={latest_snap.is_estimated}")

# 6. Market Data Ranges
print("\n[6] Market Data Timestamps & Coverage:")

# DailyStockHistory
dsh_agg = DailyStockHistory.objects.aggregate(min_date=Min('date'), max_date=Max('date'))
print(f"  - DailyStockHistory: Min={dsh_agg['min_date']}, Max={dsh_agg['max_date']}")

# MarketCandle
mc_agg = MarketCandle.objects.aggregate(min_time=Min('date_time'), max_time=Max('date_time'))
print(f"  - MarketCandle: Min={mc_agg['min_time']}, Max={mc_agg['max_time']}")

# StockTransactionTick
stt_agg = StockTransactionTick.objects.aggregate(min_date=Min('date'), max_date=Max('date'))
print(f"  - StockTransactionTick: Min={stt_agg['min_date']}, Max={stt_agg['max_date']}")

# GoldCurrencyHistory
gch_agg = GoldCurrencyHistory.objects.aggregate(min_date=Min('date'), max_date=Max('date'))
print(f"  - GoldCurrencyHistory: Min={gch_agg['min_date']}, Max={gch_agg['max_date']}")

# Prices
price_agg = Price.objects.aggregate(min_time=Min('fetched_at'), max_time=Max('fetched_at'))
print(f"  - Price (Fetched Prices): Min={price_agg['min_time']}, Max={price_agg['max_time']}")

# 7. SystemLogEvent summary
print("\n[7] System Log Events Summary:")
level_counts = SystemLogEvent.objects.values('level').annotate(count=Count('id'))
for l in level_counts:
    print(f"  - Level {l['level']}: {l['count']} events")

# 8. RejectedRecord summary
print("\n[8] Rejected Records Summary:")
rejected_reasons = RejectedRecord.objects.values('endpoint', 'reason').annotate(count=Sum('occurrences')).order_by('-count')[:10]
for r in rejected_reasons:
    print(f"  - Endpoint: {r['endpoint']} | Reason: {r['reason']} | Occurrences: {r['count']}")

# 9. Errors breakdown from 2026-07-27
print("\n[9] Log Errors/Warnings from 2026-07-27 (Yesterday):")
yesterday_errors = SystemLogEvent.objects.filter(
    level__in=['ERROR', 'WARNING'],
    timestamp__date='2026-07-27'
).values('level', 'logger_name', 'message').annotate(count=Count('id')).order_by('-count')[:10]

if yesterday_errors:
    for err in yesterday_errors:
        print(f"  - [{err['level']}] Logger={err['logger_name']} | Count={err['count']} | Msg={err['message'][:150]}")
else:
    print("  No errors found on 2026-07-27.")

print("\n" + "=" * 60)

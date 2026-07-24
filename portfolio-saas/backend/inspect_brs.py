import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings
from marketdata.fetchers.gold_currency import fetch_gold_currency_pro_history_daily
from marketdata.fetchers import fetch_gold_currency_free

print("BRS_API_KEY is:", settings.BRS_API_KEY)

# Let's check free symbol list from BRS
free_symbols = fetch_gold_currency_free(settings.BRS_API_KEY)
print("\n=== USDT/Tether symbols in BRS Free Symbol List ===")
if free_symbols:
    for group, items in free_symbols.items():
        for item in items:
            if "USDT" in item.get("symbol", "") or "تتر" in item.get("name", ""):
                print(f"Group: {group}, Symbol: {item.get('symbol')}, Name: {item.get('name')}")

print("\n=== Fetching history for USDT ===")
res_usdt = fetch_gold_currency_pro_history_daily(settings.BRS_API_KEY, "USDT")
if res_usdt:
    print("USDT payload keys:", res_usdt.keys())
    if "history_daily" in res_usdt:
        print(f"USDT history daily rows: {len(res_usdt['history_daily'])}")
        if res_usdt['history_daily']:
            print("First row:", res_usdt['history_daily'][0])
else:
    print("USDT returned None/error")

print("\n=== Fetching history for USDT_IRT ===")
res_usdt_irt = fetch_gold_currency_pro_history_daily(settings.BRS_API_KEY, "USDT_IRT")
if res_usdt_irt:
    print("USDT_IRT payload keys:", res_usdt_irt.keys())
    if "history_daily" in res_usdt_irt:
        print(f"USDT_IRT history daily rows: {len(res_usdt_irt['history_daily'])}")
        if res_usdt_irt['history_daily']:
            print("First row:", res_usdt_irt['history_daily'][0])
else:
    print("USDT_IRT returned None/error")

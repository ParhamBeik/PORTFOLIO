import os
import sys
import django

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db.models import Count, Q
from marketdata.models import (
    ArchiveFetchState,
    DailyStockHistory,
    GoldCurrencyHistory,
    CryptoHistory,
    MarketCandle,
    StockTransactionTick,
    ShareholderRecord,
    CodalAnnouncement,
)

# Output directory in backend/../test (which maps to /app/test or project-root/test)
# Since the script is run in backend, backend/../test is project-root/test.
backend_dir = os.path.dirname(os.path.abspath(__file__))
output_dir = os.path.join(backend_dir, "test")
os.makedirs(output_dir, exist_ok=True)
output_file = os.path.join(output_dir, "db_inspect.txt")

with open(output_file, "w", encoding="utf-8") as f:
    f.write("=== DATABASE DATA INSPECTION REPORT ===\n\n")

    # 1. Archive Fetch States Overview (Fetched vs Unfetched symbols)
    f.write("--- 1. Archive Fetch States (Status of Backfills) ---\n")
    states = ArchiveFetchState.objects.all().order_by("endpoint", "symbol")
    f.write(f"Total Archive states: {states.count()}\n")
    f.write(f"Verified Complete states: {states.filter(verified_complete=True).count()}\n")
    f.write(f"Pending/Unfetched states: {states.filter(verified_complete=False).count()}\n\n")

    # Group by endpoint and show completion progress
    f.write("Progress by Endpoint:\n")
    for r in states.values("endpoint").annotate(
        total=Count("id"),
        complete=Count("id", filter=Q(verified_complete=True))
    ).order_by():
        f.write(f"  - {r['endpoint']}: {r['complete']} / {r['total']} complete\n")
    f.write("\n")

    # List some unattempted/unfetched symbols per endpoint
    f.write("Sample Unfetched Symbols per Endpoint (max 10 each):\n")
    for endpoint_choice in ArchiveFetchState.Endpoint.values:
        unfetched_syms = list(
            states.filter(endpoint=endpoint_choice, last_attempt_at__isnull=True)
            .values_list("symbol", flat=True)[:10]
        )
        if unfetched_syms:
            f.write(f"  - {endpoint_choice}: {', '.join(unfetched_syms)}\n")
    f.write("\n")

    # Helper to dump SQL-like tables
    def dump_table_sample(model_class, limit=50):
        f.write(f"--- Table: {model_class._meta.db_table} (Sample max {limit} rows) ---\n")
        fields = [field.name for field in model_class._meta.fields]
        f.write(" | ".join(fields) + "\n")
        f.write("-" * 80 + "\n")
        
        rows = model_class.objects.all()[:limit]
        for row in rows:
            row_vals = []
            for field in fields:
                val = getattr(row, field)
                row_vals.append(str(val))
            f.write(" | ".join(row_vals) + "\n")
        f.write(f"Total count in table: {model_class.objects.count()} rows\n\n")

    # Dump samples of all relevant tables
    dump_table_sample(CryptoHistory)
    dump_table_sample(GoldCurrencyHistory, limit=30)
    dump_table_sample(DailyStockHistory, limit=30)
    dump_table_sample(MarketCandle, limit=30)
    dump_table_sample(StockTransactionTick, limit=30)
    dump_table_sample(ShareholderRecord, limit=30)
    dump_table_sample(CodalAnnouncement, limit=30)

print(f"Inspection report written to {output_file}")

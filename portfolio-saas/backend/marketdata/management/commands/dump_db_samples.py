import os
from django.core.management.base import BaseCommand
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

class Command(BaseCommand):
    help = "Dump sample rows from database tables to inspect format and content."

    def add_arguments(self, parser):
        parser.add_argument(
            '--stdout',
            action='store_true',
            help='Output to stdout instead of file',
        )

    def handle(self, *args, **options):
        output_file = "/app/test/db_inspect.txt"
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        # We can write to a file or stdout
        if options['stdout']:
            import sys
            out = sys.stdout
        else:
            out = open(output_file, "w", encoding="utf-8")

        try:
            out.write("=== DATABASE DATA INSPECTION REPORT ===\n\n")

            # 1. Archive Fetch States Overview (Fetched vs Unfetched symbols)
            out.write("--- 1. Archive Fetch States (Status of Backfills) ---\n")
            states = ArchiveFetchState.objects.all().order_by("endpoint", "symbol")
            out.write(f"Total Archive states: {states.count()}\n")
            out.write(f"Verified Complete states: {states.filter(verified_complete=True).count()}\n")
            out.write(f"Pending/Unfetched states: {states.filter(verified_complete=False).count()}\n\n")

            # Group by endpoint and show completion progress
            out.write("Progress by Endpoint:\n")
            for r in states.values("endpoint").annotate(
                total=Count("id"),
                complete=Count("id", filter=Q(verified_complete=True))
            ).order_by():
                out.write(f"  - {r['endpoint']}: {r['complete']} / {r['total']} complete\n")
            out.write("\n")

            # List some unattempted/unfetched symbols per endpoint
            out.write("Sample Unfetched Symbols per Endpoint (max 10 each):\n")
            for endpoint_choice in ArchiveFetchState.Endpoint.values:
                unfetched_syms = list(
                    states.filter(endpoint=endpoint_choice, last_attempt_at__isnull=True)
                    .values_list("symbol", flat=True)[:10]
                )
                if unfetched_syms:
                    out.write(f"  - {endpoint_choice}: {', '.join(unfetched_syms)}\n")
            out.write("\n")

            # Helper to dump SQL-like tables
            def dump_table_sample(model_class, limit=50):
                out.write(f"--- Table: {model_class._meta.db_table} (Sample max {limit} rows) ---\n")
                fields = [field.name for field in model_class._meta.fields]
                out.write(" | ".join(fields) + "\n")
                out.write("-" * 80 + "\n")
                
                rows = model_class.objects.all()[:limit]
                for row in rows:
                    row_vals = []
                    for field in fields:
                        val = getattr(row, field)
                        row_vals.append(str(val))
                    out.write(" | ".join(row_vals) + "\n")
                out.write(f"Total count in table: {model_class.objects.count()} rows\n\n")

            # Dump samples of all relevant tables
            dump_table_sample(CryptoHistory)
            dump_table_sample(GoldCurrencyHistory, limit=30)
            dump_table_sample(DailyStockHistory, limit=30)
            dump_table_sample(MarketCandle, limit=30)
            dump_table_sample(StockTransactionTick, limit=30)
            dump_table_sample(ShareholderRecord, limit=30)
            dump_table_sample(CodalAnnouncement, limit=30)

            if not options['stdout']:
                self.stdout.write(self.style.SUCCESS(f"Inspection report written to {output_file}"))
        finally:
            if not options['stdout']:
                out.close()

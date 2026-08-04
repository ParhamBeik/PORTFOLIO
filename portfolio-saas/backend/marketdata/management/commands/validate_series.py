import time
from django.core.management.base import BaseCommand
from django.db import connection
from marketdata.tasks import nightly_series_validation
from marketdata.models import RejectedRecord

class Command(BaseCommand):
    help = "Dry-run or run nightly series validation (F2/F3) with detailed logging."

    def add_arguments(self, parser):
        parser.add_argument(
            "--symbols",
            type=str,
            help="Comma-separated TSE symbols to validate",
        )
        parser.add_argument(
            "--gold-symbols",
            type=str,
            help="Comma-separated Gold/Currency symbols to validate",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually apply changes to the database (defaults to dry-run)",
        )

    def handle(self, *args, **options):
        apply_mode = options["apply"]
        dry_run = not apply_mode

        # Parse symbols
        if options["symbols"]:
            symbols = [s.strip() for s in options["symbols"].split(",") if s.strip()]
        else:
            # Default representative sample
            symbols = ["آواپارس"]

        if options["gold_symbols"]:
            gold_symbols = [s.strip() for s in options["gold_symbols"].split(",") if s.strip()]
        else:
            # Default representative sample: SEK, two other affected, one normal (USD)
            gold_symbols = ["SEK", "CNY", "MYR", "USD"]

        self.stdout.write(self.style.NOTICE(f"=== {'APPLY' if apply_mode else 'DRY RUN'} SERIES VALIDATION ==="))
        self.stdout.write(f"TSE Symbols to examine: {symbols}")
        self.stdout.write(f"Gold/Currency Symbols to examine: {gold_symbols}")

        start_time = time.time()
        start_queries = len(connection.queries)

        # Execute validation task in dry_run or write mode
        results = nightly_series_validation(
            dry_run=dry_run,
            symbols=symbols,
            gold_symbols=gold_symbols
        )

        end_time = time.time()
        end_queries = len(connection.queries)

        runtime = end_time - start_time
        query_count = end_queries - start_queries

        # Print detailed report
        self.stdout.write("\n=== Corporate Action Candidates ===")
        candidates = results["corporate_action_candidates"]
        for c in candidates:
            self.stdout.write(
                f"  Symbol: {c['symbol']}, Date: {c['date']}, "
                f"Factor: {c['factor']:.4f}, Status: {c['status']} ({c['reason']})"
            )
        self.stdout.write(f"Total corporate action candidates found: {len(candidates)}")

        self.stdout.write("\n=== Proposed/Detected Spikes (Rejection Records) ===")
        rejections = results["proposed_rejections"]
        
        # Check for existing duplicate findings
        duplicates_count = 0
        for r in rejections:
            # Check if this exact rejection already exists in the database
            exists = RejectedRecord.objects.filter(
                symbol=r["symbol"],
                date=r["date"],
                endpoint=r["endpoint"],
                reason=r["reason"]
            ).exists()
            dup_str = " [ALREADY EXISTS]" if exists else ""
            if exists:
                duplicates_count += 1
                
            self.stdout.write(
                f"  Symbol: {r['symbol']}, Date: {r['date']}, "
                f"Endpoint: {r['endpoint']}, Reason: {r['reason']}{dup_str}"
            )
            # Sample payload data
            payload = r["payload"]
            self.stdout.write(
                f"    Detail: close={payload.get('close')}, "
                f"previous_close={payload.get('previous_close')}, "
                f"log_return={payload.get('log_return', 0.0):.4f}"
            )
            
        self.stdout.write(f"Total spikes detected: {len(rejections)}")
        self.stdout.write(f"Existing duplicate findings: {duplicates_count}")

        self.stdout.write("\n=== Summary ===")
        self.stdout.write(f"TSE Symbols Examined: {results['tse_symbols_examined']}")
        self.stdout.write(f"Corporate Actions {'Created' if apply_mode else 'Proposed'}: {results['actions_created']}")
        self.stdout.write(f"Gold/Currency Symbols Examined: {results['gold_currency_symbols_examined']}")
        self.stdout.write(f"Spikes {'Rejected' if apply_mode else 'Proposed'}: {results['spikes_rejected']}")
        self.stdout.write(f"Database rows changed: {0 if dry_run else (results['actions_created'] + results['spikes_rejected'])}")
        self.stdout.write(f"Queries executed: {query_count}")
        self.stdout.write(f"Execution runtime: {runtime:.4f} seconds")

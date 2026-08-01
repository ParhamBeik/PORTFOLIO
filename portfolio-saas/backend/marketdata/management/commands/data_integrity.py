from django.core.management.base import BaseCommand
from marketdata.integrity import update_all_symbols_integrity

class Command(BaseCommand):
    help = "Compute and save data integrity results for all symbols."

    def handle(self, *args, **options):
        self.stdout.write("Starting data integrity check...")
        results = update_all_symbols_integrity()
        passes = sum(1 for r in results if r.passes_gate)
        fails = len(results) - passes
        self.stdout.write(self.style.SUCCESS(f"Data integrity check complete. Passes: {passes}, Fails: {fails}"))

"""Fill TickDaySummary for symbols that have no coverage yet (VPS load fix P4).

Each symbol costs one full read of its ticks -- the read the summary exists to
avoid -- so run it outside market hours (09:00-12:30 Tehran, Sat-Wed). It is
resumable: a covered symbol is skipped, and every symbol commits on its own.

    manage.py backfill_tick_summary --limit 200
    manage.py backfill_tick_summary --symbol فملی --force
"""
import time

from django.core.management.base import BaseCommand

from marketdata import ingest
from marketdata.models import ArchiveFetchState, TickSummaryCoverage


class Command(BaseCommand):
    help = "Backfill the per-day tick summary, one symbol at a time."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", action="append", default=[], help="Only these symbols (repeatable).")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many symbols (0 = all).")
        parser.add_argument("--force", action="store_true", help="Rebuild symbols already covered.")

    def handle(self, *args, symbol, limit, force, **options):
        symbols = symbol or sorted(set(
            ArchiveFetchState.objects.filter(endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS)
            .values_list("symbol", flat=True)
        ))
        if not force:
            covered = set(TickSummaryCoverage.objects.values_list("symbol", flat=True))
            symbols = [s for s in symbols if s not in covered]
        if limit:
            symbols = symbols[:limit]
        total_days = 0
        for index, name in enumerate(symbols, 1):
            started = time.monotonic()
            days = ingest.backfill_tick_summary(name)
            total_days += days
            self.stdout.write(f"[{index}/{len(symbols)}] {name}: {days} days in {time.monotonic() - started:.1f}s")
        self.stdout.write(self.style.SUCCESS(f"{len(symbols)} symbols, {total_days} days summarised."))

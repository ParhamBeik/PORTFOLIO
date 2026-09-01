"""Backfill daily TEDPIX history from TGJU without spending BrsApi quota."""
from django.core.management.base import BaseCommand

from marketdata.ingest import ingest_tedpix_history
from marketdata.sources import tgju
from marketdata.sources.http import SourceError


class Command(BaseCommand):
    help = "Backfill daily TEDPIX history from TGJU (unmetered)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and validate without writing rows.",
        )

    def handle(self, *args, **options):
        try:
            records = tgju.tedpix_history_rows(tgju.fetch_tedpix_history())
        except SourceError as exc:
            self.stderr.write(self.style.ERROR(f"TGJU unavailable: {exc}"))
            return
        if not records:
            self.stderr.write(self.style.ERROR("TGJU returned no valid TEDPIX rows."))
            return

        first, last = records[-1]["jalali"], records[0]["jalali"]
        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"TEDPIX dry run: {len(records)} trading days, {first}..{last}"
                )
            )
            return

        created, known_or_rejected = ingest_tedpix_history(records)
        self.stdout.write(
            self.style.SUCCESS(
                f"TEDPIX backfill: {created} new, {known_or_rejected} "
                f"known/rejected, {first}..{last}"
            )
        )

"""Fetch market prices and persist them.

Thin wrapper around `portfolio.tasks.run_price_fetch` so the management command
(GitHub Actions' dead-man's switch) and Celery beat share one code path. Calling
the command runs the fetch synchronously and in-process, which is what a CI
runner without a broker needs.
"""
from django.core.management.base import BaseCommand

from portfolio.tasks import run_price_fetch


class Command(BaseCommand):
    help = "Fetch live market prices, store them, and snapshot every user's valuation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Fetch and extract but do not write prices or snapshots.",
        )

    def handle(self, *args, dry_run=False, **options):
        result = run_price_fetch(dry_run=dry_run)
        priced = result["priced"]
        self.stdout.write(f"Fetched {len(priced)} positive prices: {sorted(priced)}")

        if dry_run or not priced:
            self.stdout.write("Dry run or no prices; nothing written.")
            return

        self.stdout.write(self.style.SUCCESS("Price fetch complete."))

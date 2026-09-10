from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from portfolio.models import Holding, Transaction
from portfolio.services.valuation import get_latest_prices


class Command(BaseCommand):
    help = "Backfill transaction history for holdings that have no ledger rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--price-source",
            choices=["latest", "zero", "manual"],
            required=True,
            help="Source for the buy price of the trade.",
        )
        parser.add_argument(
            "--price",
            type=float,
            help="Price to use if price-source is manual.",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Commit the changes to the database (default is dry-run).",
        )

    def handle(self, *args, **options):
        price_source = options["price_source"]
        manual_price = options["price"]
        commit = options["commit"]

        if price_source == "manual" and manual_price is None:
            raise CommandError("A --price must be specified when using --price-source=manual.")

        if price_source == "manual" and manual_price < 0:
            raise CommandError("Price cannot be negative.")

        # If price-source is latest, pre-load latest prices.
        latest_prices = {}
        if price_source == "latest":
            latest_prices = get_latest_prices()

        self.stdout.write("Scanning for holdings with no transaction history...")

        # We want to identify holdings that have no Transactions for their account & asset.
        # excludes is_house assets as they are manual / not tradeable.
        holdings = Holding.objects.filter(asset__is_house=False).select_related("account", "asset")

        orphaned = []
        for holding in holdings:
            has_tx = Transaction.objects.filter(account=holding.account, asset=holding.asset).exists()
            if not has_tx:
                orphaned.append(holding)

        if not orphaned:
            self.stdout.write(self.style.SUCCESS("No orphaned holdings found. All holdings have transaction history."))
            return

        self.stdout.write(f"Found {len(orphaned)} orphaned holdings.")

        try:
            with transaction.atomic():
                for holding in orphaned:
                    # Determine price
                    if price_source == "latest":
                        price = latest_prices.get(holding.asset.key, 0)
                        if not price or price <= 0:
                            raise CommandError(
                                f"No latest price available for asset {holding.asset.key}. "
                                "Use --price-source=zero or manual instead."
                            )
                    elif price_source == "zero":
                        price = 0
                    else:  # manual
                        price = manual_price

                    qty = holding.quantity
                    self.stdout.write(
                        f"Backfilling {holding.asset.key} in account {holding.account.name} "
                        f"(qty={qty}) with price={price}..."
                    )

                    # Opening position establishes quantity without inventing a
                    # purchase that would also debit cash. Drop the orphan row
                    # first so the ledger projection can recreate it cleanly.
                    holding_qty = holding.quantity
                    account = holding.account
                    asset = holding.asset
                    holding.delete()

                    from portfolio.services.ledger import create_ledger_entry
                    from portfolio.models import LedgerEntry

                    # Openings on one account must share tracking_started_at.
                    occurred_at = account.tracking_started_at
                    create_ledger_entry(
                        account=account,
                        kind=LedgerEntry.Kind.OPENING_POSITION,
                        asset=asset,
                        quantity=holding_qty,
                        occurred_at=occurred_at,
                        source="system",
                        note="Backfilled opening balance",
                    )

                if not commit:
                    self.stdout.write(self.style.WARNING("DRY RUN: rolling back changes. Use --commit to save."))
                    transaction.set_rollback(True)
                else:
                    self.stdout.write(self.style.SUCCESS("Changes committed successfully."))

        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"Error executing backfill: {exc}"))
            raise exc

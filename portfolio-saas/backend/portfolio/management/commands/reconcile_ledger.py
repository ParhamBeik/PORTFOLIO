"""Rebuild or verify Holding and cash projections from the immutable ledger."""
import sys

from django.core.management.base import BaseCommand

from portfolio.models import Account, Asset
from portfolio.services.ledger import projection_drift, rebuild_projections


class Command(BaseCommand):
    help = (
        "Verify (default) or rebuild Holding quantities and cash_balance_tomans "
        "from LedgerEntry rows. Reversal pairs are netted out."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Rewrite projections from the ledger instead of only reporting drift.",
        )
        parser.add_argument(
            "--account-id",
            type=int,
            default=None,
            help="Limit to one account id.",
        )

    def handle(self, *args, **options):
        qs = Account.objects.all().order_by("id")
        if options["account_id"] is not None:
            qs = qs.filter(pk=options["account_id"])

        has_drift = False
        fixed = 0
        for account in qs:
            drifts = projection_drift(account)
            if not drifts:
                continue
            has_drift = True
            for drift in drifts:
                if drift["kind"] == "cash":
                    self.stderr.write(
                        f"Cash drift account={account.id} ({account.name}): "
                        f"stored={drift['stored']} ledger={drift['ledger']}"
                    )
                else:
                    asset = Asset.objects.filter(pk=drift["asset_id"]).first()
                    label = asset.key if asset else drift["asset_id"]
                    self.stderr.write(
                        f"Holding drift account={account.id} asset={label}: "
                        f"stored={drift['stored']} ledger={drift['ledger']}"
                    )
            if options["fix"]:
                rebuild_projections(account)
                fixed += 1
                self.stdout.write(
                    self.style.WARNING(f"Rebuilt projections for account {account.id}")
                )

        if options["fix"]:
            remaining = False
            for account in qs:
                account.refresh_from_db()
                if projection_drift(account):
                    remaining = True
                    break
            if remaining:
                self.stderr.write("Reconciliation still reports drift after --fix.")
                sys.exit(1)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Rebuilt {fixed} account(s); all projections match the ledger."
                )
            )
            return

        if has_drift:
            self.stderr.write("Reconciliation failed due to ledger drift.")
            sys.exit(1)
        self.stdout.write(
            self.style.SUCCESS("All holdings and cash match the ledger.")
        )

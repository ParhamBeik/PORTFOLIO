import hashlib
import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from portfolio.models import Account, Asset, Holding, LedgerEntry
from portfolio.services.ledger import create_ledger_entry


class Command(BaseCommand):
    help = "Seed representative restore data and print deterministic evidence."

    def add_arguments(self, parser):
        parser.add_argument("--seed", action="store_true")

    def handle(self, *args, **options):
        if options["seed"]:
            self._seed()
        tables = {
            "users": User.objects.order_by("id").values_list("email", "tier"),
            "accounts": Account.objects.order_by("id").values_list(
                "user__email", "name", "cash_balance_tomans"
            ),
            "ledger": LedgerEntry.objects.order_by("id").values_list(
                "account__name", "kind", "quantity", "amount_tomans"
            ),
            "holdings": Holding.objects.order_by("id").values_list(
                "account__name", "asset__key", "quantity"
            ),
        }
        normalized = {
            name: [[str(value) for value in row] for row in rows]
            for name, rows in tables.items()
        }
        payload = {
            "counts": {name: len(rows) for name, rows in normalized.items()},
            "checksum": hashlib.sha256(
                json.dumps(normalized, sort_keys=True).encode()
            ).hexdigest(),
        }
        self.stdout.write(json.dumps(payload, sort_keys=True))

    def _seed(self):
        user, _ = User.objects.get_or_create(
            email="restore-drill@test.local",
            defaults={"email_verified_at": timezone.now()},
        )
        if not user.has_usable_password():
            user.set_unusable_password()
            user.save(update_fields=["password"])
        account, _ = Account.objects.get_or_create(user=user, name="Restore Drill")
        asset, _ = Asset.objects.get_or_create(
            key="restore_drill_asset",
            defaults={"name": "Restore Drill Asset", "is_active": False},
        )
        if not account.transactions.exists():
            opened_at = timezone.now()
            create_ledger_entry(
                account=account,
                kind=LedgerEntry.Kind.OPENING_CASH,
                amount_tomans=1000,
                occurred_at=opened_at,
                source="system",
            )
            create_ledger_entry(
                account=account,
                kind=LedgerEntry.Kind.OPENING_POSITION,
                asset=asset,
                quantity=2,
                occurred_at=opened_at,
                source="system",
            )

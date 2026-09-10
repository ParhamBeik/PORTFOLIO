"""Create a demo user and E2E test users with accounts, holdings, and seed prices.

Lets the app show real data immediately on first boot, before the price cron
has run. Idempotent: only creates the demo and E2E users if they don't exist.
DEBUG-only.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from portfolio.models import Account, Asset, Holding, Price
from portfolio.services.ledger import create_ledger_entry
from portfolio.models import LedgerEntry

DEMOFREE_EMAIL = "demofree@portfolio.local"
DEMOFREE_PASSWORD = "demofree12345"

DEMOPRO_EMAIL = "demopro@portfolio.local"
DEMOPRO_PASSWORD = "demopro12345"

ADMIN_EMAIL = "admin@portfolio.local"
ADMIN_PASSWORD = "admin12345"


class Command(BaseCommand):
    help = "Create demofree, demopro, and admin users with sample holdings and prices."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            self.stdout.write("Skipping demo/E2E users (DEBUG=False).")
            return

        # Seed prices first (shared across all users)
        seed_prices = {
            "emami_coin": 176000000,
            "half_coin": 92800000,
            "quarter_coin": 52900000,
            "one_gram_coin": 26100000,
            "gold_18k_gram": 6400000,
            "kama_stock": 1780,
        }
        holdings = {
            "emami_coin": 5,
            "quarter_coin": 8,
            "one_gram_coin": 4,
            "kama_stock": 100000,
        }
        all_asset_keys = set(holdings.keys()) | set(seed_prices.keys())
        asset_map = {
            a.key: a for a in Asset.objects.filter(key__in=all_asset_keys, is_active=True)
        }

        # Seed realistic-ish Tomans prices if they don't exist yet
        for k, v in seed_prices.items():
            if k in asset_map:
                Price.objects.get_or_create(
                    asset=asset_map[k],
                    price=v,
                    defaults={"source": "SEED"}
                )

        # 1. Demo Free User
        demofree_user, created = User.objects.get_or_create(
            email=DEMOFREE_EMAIL,
            defaults={
                "first_name": "Demo",
                "last_name": "Free",
                "is_active": True,
            },
        )
        if created:
            demofree_user.set_password(DEMOFREE_PASSWORD)
            demofree_user.save()
            # Seed a single gold position for the free user
            self._seed_holdings(demofree_user, {"gold_18k_gram": 10}, asset_map)
            self.stdout.write(self.style.SUCCESS(f"Demo Free ready -> {DEMOFREE_EMAIL}"))

        # 2. Demo Pro User
        demopro_user, created = User.objects.get_or_create(
            email=DEMOPRO_EMAIL,
            defaults={
                "first_name": "Demo",
                "last_name": "Pro",
                "is_active": True,
            },
        )
        if created:
            demopro_user.set_password(DEMOPRO_PASSWORD)
            demopro_user.save()
            self._seed_holdings(demopro_user, holdings, asset_map)
            self.stdout.write(self.style.SUCCESS(f"Demo Pro ready -> {DEMOPRO_EMAIL}"))

        # 3. Admin User
        admin_user, created = User.objects.get_or_create(
            email=ADMIN_EMAIL,
            defaults={
                "first_name": "Admin",
                "last_name": "User",
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            },
        )
        if created:
            admin_user.set_password(ADMIN_PASSWORD)
            admin_user.save()
            Account.objects.get_or_create(user=admin_user, name="Main Portfolio")
            self.stdout.write(self.style.SUCCESS(f"Admin ready -> {ADMIN_EMAIL}"))



    def _seed_holdings(self, user, holdings, asset_map):
        account = Account.objects.create(user=user, name="Main Portfolio")
        opened_at = timezone.now()

        create_ledger_entry(
            account=account,
            kind=LedgerEntry.Kind.OPENING_CASH,
            amount_tomans="1000000000",
            occurred_at=opened_at,
            source="system",
            note="Opening cash",
        )
        for key, qty in holdings.items():
            if key in asset_map:
                asset = asset_map[key]
                if asset.is_house:
                    Holding.objects.create(account=account, asset=asset, quantity=qty)
                else:
                    create_ledger_entry(
                        account=account,
                        kind=LedgerEntry.Kind.OPENING_POSITION,
                        asset=asset,
                        quantity=qty,
                        occurred_at=opened_at,
                        source="system",
                        note="Opening position",
                    )

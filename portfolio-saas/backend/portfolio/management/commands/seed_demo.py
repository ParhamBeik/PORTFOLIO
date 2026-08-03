"""Create a demo user and E2E test users with accounts, holdings, and seed prices.

Lets the app show real data immediately on first boot, before the price cron
has run. Idempotent: only creates the demo and E2E users if they don't exist.
DEBUG-only.
"""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from portfolio.models import Account, Asset, Holding, Price
from portfolio.services.ledger import create_ledger_entry
from portfolio.models import LedgerEntry

DEMO_EMAIL = "demo@portfolio.local"
DEMO_PASSWORD = "demo12345"
E2E_PASSWORD = "Sup3rSecret!"


class Command(BaseCommand):
    help = "Create a demo user and E2E users with sample holdings and prices."

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

        # 1. Demo User
        demo_user, created = User.objects.get_or_create(
            email=DEMO_EMAIL,
            defaults={
                "first_name": "Demo",
                "last_name": "User",
                "tier": User.Tier.PRO,
                "email_verified_at": timezone.now(),
                "is_active": True,
            },
        )
        if created:
            demo_user.set_password(DEMO_PASSWORD)
            demo_user.save()
            self._seed_holdings(demo_user, holdings, asset_map)
            self.stdout.write(self.style.SUCCESS(f"Demo ready -> {DEMO_EMAIL}"))

        # 2. E2E Free User
        free_user, created = User.objects.get_or_create(
            email="e2e-free@portfolio.local",
            defaults={
                "first_name": "E2E",
                "last_name": "Free",
                "tier": User.Tier.FREE,
                "email_verified_at": timezone.now(),
                "is_active": True,
            },
        )
        if created:
            free_user.set_password(E2E_PASSWORD)
            free_user.save()
            Account.objects.create(user=free_user, name="Main Portfolio")
            self.stdout.write(self.style.SUCCESS("E2E Free user ready"))

        # 3. E2E Pro User — stamp an expiry so Billing shows "until <date>"
        # (None expiry is a valid grant, but the e2e suite asserts the dated copy).
        pro_expires = timezone.now() + timezone.timedelta(days=365)
        pro_user, created = User.objects.get_or_create(
            email="e2e-pro@portfolio.local",
            defaults={
                "first_name": "E2E",
                "last_name": "Pro",
                "tier": User.Tier.PRO,
                "pro_expires_at": pro_expires,
                "email_verified_at": timezone.now(),
                "is_active": True,
            },
        )
        if created:
            pro_user.set_password(E2E_PASSWORD)
            pro_user.save()
            self._seed_holdings(pro_user, holdings, asset_map)
            self.stdout.write(self.style.SUCCESS("E2E Pro user ready"))
        elif pro_user.tier != User.Tier.PRO or pro_user.pro_expires_at is None:
            pro_user.tier = User.Tier.PRO
            pro_user.pro_expires_at = pro_expires
            pro_user.save(update_fields=["tier", "pro_expires_at"])
            self.stdout.write(self.style.SUCCESS("E2E Pro user expiry refreshed"))

        # 4. E2E Admin User
        admin_user, created = User.objects.get_or_create(
            email="e2e-admin@portfolio.local",
            defaults={
                "first_name": "E2E",
                "last_name": "Admin",
                "tier": User.Tier.PRO,
                "is_staff": True,
                "is_superuser": True,
                "email_verified_at": timezone.now(),
                "is_active": True,
            },
        )
        if created:
            admin_user.set_password(E2E_PASSWORD)
            admin_user.save()
            self.stdout.write(self.style.SUCCESS("E2E Admin user ready"))

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

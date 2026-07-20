"""Create a demo user with an account, holdings, and seed prices.

Lets the app show real data immediately on first boot, before the price cron
has run. Idempotent: only creates the demo if it doesn't exist. DEBUG-only (M6):
a PRO demo user must never appear on a production boot.
"""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand

from accounts.models import User
from portfolio.models import Account, Asset, Holding, Price

DEMO_EMAIL = "demo@portfolio.local"
DEMO_PASSWORD = "demo12345"


class Command(BaseCommand):
    help = "Create a demo user with sample holdings and prices."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            self.stdout.write("Skipping demo user (DEBUG=False).")
            return

        user, created = User.objects.get_or_create(
            email=DEMO_EMAIL,
            defaults={"first_name": "Demo", "last_name": "User", "tier": User.Tier.PRO},
        )
        if not created:
            self.stdout.write("Demo user already exists; skipping.")
            return

        user.set_password(DEMO_PASSWORD)
        user.save()

        account = Account.objects.create(user=user, name="Main Portfolio")

        holdings = {
            "emami_coin": 5,
            "quarter_coin": 8,
            "one_gram_coin": 4,
            "usd_cash": 2000,
            "kama_stock": 100000,
            "bitcoin_usd": 0.05,
        }
        asset_map = {a.key: a for a in Asset.objects.filter(key__in=holdings)}
        for key, qty in holdings.items():
            if key in asset_map:
                Holding.objects.create(account=account, asset=asset_map[key], quantity=qty)

        # Seed realistic-ish Tomans prices so valuation is non-zero pre-cron.
        seed_prices = {
            "emami_coin": 176000000,
            "half_coin": 92800000,
            "quarter_coin": 52900000,
            "quarter_coin_pre86": 46000000,
            "one_gram_coin": 26100000,
            "swiss_gold_bar_1g": 25900000,
            "swiss_gold_bar_2_5g": 61610000,
            "gold_18k_gram": 6400000,
            "usd_cash": 92000,
            "usdt_irt": 91500,
            "euro_cash": 99000,
            "kama_stock": 1780,
            "bitcoin_usd": 6500000000,
            "gold_ounce_usd": 24000000,
        }
        asset_map = {a.key: a for a in Asset.objects.filter(key__in=seed_prices)}
        Price.objects.bulk_create([
            Price(asset=asset_map[k], price=v, source="SEED")
            for k, v in seed_prices.items() if k in asset_map
        ])

        self.stdout.write(self.style.SUCCESS(
            f"Demo ready -> {DEMO_EMAIL} / {DEMO_PASSWORD} (Pro tier)"
        ))

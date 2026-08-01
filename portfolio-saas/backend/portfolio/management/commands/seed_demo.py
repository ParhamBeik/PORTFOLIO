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
            "kama_stock": 100000,
        }

        # Seed realistic-ish Tomans prices so valuation is non-zero pre-cron.
        seed_prices = {
            "emami_coin": 176000000,
            "half_coin": 92800000,
            "quarter_coin": 52900000,
            "one_gram_coin": 26100000,
            "gold_18k_gram": 6400000,
            "kama_stock": 1780,
        }
        all_asset_keys = set(holdings.keys()) | set(seed_prices.keys())
        asset_map = {
            a.key: a for a in Asset.objects.filter(key__in=all_asset_keys, is_active=True)
        }
        Price.objects.bulk_create([
            Price(asset=asset_map[k], price=v, source="SEED")
            for k, v in seed_prices.items() if k in asset_map
        ])

        from portfolio.services.trades import execute_trade
        from portfolio.models import Transaction

        for key, qty in holdings.items():
            if key in asset_map:
                asset = asset_map[key]
                if asset.is_house:
                    Holding.objects.create(account=account, asset=asset, quantity=qty)
                else:
                    execute_trade(
                        account=account,
                        asset=asset,
                        side=Transaction.Side.BUY,
                        quantity=qty,
                        price_tomans=Decimal(str(seed_prices.get(key, 0))),
                        skip_snapshots=True,
                    )

        self.stdout.write(self.style.SUCCESS(
            f"Demo ready -> {DEMO_EMAIL} / {DEMO_PASSWORD} (Pro tier)"
        ))

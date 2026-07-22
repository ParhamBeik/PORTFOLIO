"""Management command to clean zero/negative snapshots and corrupted 04/31 stock history rows."""
from django.core.management.base import BaseCommand
from portfolio.models import Price, Snapshot
from marketdata.models import DailyStockHistory, GoldCurrencyHistory


class Command(BaseCommand):
    help = "Purge zero-value snapshots and corrupted 04/31 price history rows from the database."

    def handle(self, *args, **options):
        # 1. Purge zero/negative snapshots and anomalous partial snapshots (14B - 17B)
        bad_snaps = Snapshot.objects.filter(total_value_tomans__lte=0) | Snapshot.objects.filter(
            total_value_tomans__gte=14000000000, total_value_tomans__lte=17000000000
        )
        snap_count = bad_snaps.count()
        bad_snaps.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {snap_count} zero, negative, or anomalous 15B Snapshot records."))


        # 2. Purge 04-31 and 04/31 corrupted stock history
        corrupted_stocks = (
            DailyStockHistory.objects.filter(date__icontains="04-31")
            | DailyStockHistory.objects.filter(date__icontains="04/31")
        )
        stock_count = corrupted_stocks.count()
        corrupted_stocks.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {stock_count} corrupted 04-31/04/31 DailyStockHistory records."))

        # 3. Purge zero/negative prices
        zero_prices = Price.objects.filter(price__lte=0)
        price_count = zero_prices.count()
        zero_prices.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {price_count} zero or negative Price records."))

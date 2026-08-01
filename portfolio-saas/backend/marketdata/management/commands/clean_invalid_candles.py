"""Quarantine and optionally remove legacy candles that fail current OHLC rules."""
from django.core.management.base import BaseCommand
from django.db.models import F, Q

from marketdata import ingest
from marketdata.models import MarketCandle


class Command(BaseCommand):
    help = "Quarantine invalid legacy candles; delete them only with --apply."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Delete rows after quarantine.")

    def handle(self, *args, **options):
        invalid = MarketCandle.objects.filter(
            Q(open_price__lte=0) | Q(high_price__lte=0) | Q(low_price__lte=0)
            | Q(close_price__lte=0) | Q(high_price__lt=F("low_price"))
            | Q(open_price__lt=F("low_price")) | Q(open_price__gt=F("high_price"))
            | Q(close_price__lt=F("low_price")) | Q(close_price__gt=F("high_price"))
        )
        if not options["apply"]:
            self.stdout.write(f"{invalid.count()} invalid candles found; rerun with --apply to quarantine and delete.")
            return

        removed = 0
        for candle in invalid.iterator(chunk_size=500):
            endpoint = f"stock_candle_{'adjusted' if candle.timeframe == '1d_adj' else 'unadjusted'}"
            ingest.screen("candle", [{
                "date": candle.date_time, "open": candle.open_price,
                "high": candle.high_price, "low": candle.low_price,
                "close": candle.close_price, "volume": candle.volume,
            }], endpoint, candle.symbol)
            candle.delete()
            removed += 1
        self.stdout.write(self.style.SUCCESS(f"Quarantined and removed {removed} invalid candles."))

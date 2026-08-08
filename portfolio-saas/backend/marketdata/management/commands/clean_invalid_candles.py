"""Quarantine and clean legacy candles that fail current OHLC rules."""
from django.core.management.base import BaseCommand

from marketdata import ingest
from marketdata.models import MarketCandle


class Command(BaseCommand):
    help = "Quarantine invalid legacy candles; salvage or delete them with --apply."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Salvage valid closes and delete only irrecoverable rows.",
        )

    def handle(self, *args, **options):
        invalid = []
        for candle in MarketCandle.objects.all().iterator(chunk_size=500):
            accepted, _ = ingest.validation.validate("candle", [{
                "date": candle.date_time,
                "open": candle.open_price,
                "high": candle.high_price,
                "low": candle.low_price,
                "close": candle.close_price,
                "volume": candle.volume,
            }])
            if not accepted:
                invalid.append(candle)
        if not options["apply"]:
            self.stdout.write(f"{len(invalid)} invalid candles found; rerun with --apply to quarantine and delete.")
            return

        salvaged = removed = 0
        for candle in invalid:
            endpoint = f"stock_candle_{'adjusted' if candle.timeframe == '1d_adj' else 'unadjusted'}"
            accepted, _ = ingest.screen("candle", [{
                "date": candle.date_time, "open": candle.open_price,
                "high": candle.high_price, "low": candle.low_price,
                "close": candle.close_price, "volume": candle.volume,
            }], endpoint, candle.symbol)
            if accepted:
                row = accepted[0]
                candle.open_price = row.get("open")
                candle.high_price = row.get("high")
                candle.low_price = row.get("low")
                candle.save(update_fields=("open_price", "high_price", "low_price"))
                salvaged += 1
            else:
                candle.delete()
                removed += 1
        self.stdout.write(self.style.SUCCESS(
            f"Quarantined {len(invalid)} invalid candles; "
            f"salvaged {salvaged}, removed {removed}."
        ))

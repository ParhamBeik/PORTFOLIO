from django.core.management.base import BaseCommand
from django.db.models import Max, Min, Sum
from django.utils import timezone

from marketdata.models import ArchiveFetchState, MarketCandle, StockTransactionTick


class Command(BaseCommand):
    help = "Audit tick-derived rows that may occupy authoritative adjusted slots."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        apply = options["apply"]
        suspects = []
        rows = MarketCandle.objects.filter(
            timeframe=MarketCandle.ADJUSTED
        ).order_by("symbol", "date_time")
        states = {
            state.symbol: state
            for state in ArchiveFetchState.objects.filter(
                endpoint=ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED
            )
        }
        for candle in rows.iterator():
            ticks = StockTransactionTick.objects.filter(
                symbol=candle.symbol, date=candle.date_time
            ).order_by("row")
            if not ticks.exists():
                continue
            aggregate = ticks.aggregate(
                high=Max("price"), low=Min("price"), volume=Sum("volume")
            )
            state = states.get(candle.symbol)
            matches_ticks = (
                candle.open_price == ticks.first().price
                and candle.close_price == ticks.last().price
                and candle.high_price == aggregate["high"]
                and candle.low_price == aggregate["low"]
                and candle.volume == (aggregate["volume"] or 0)
            )
            no_fetch_record = (
                state is None
                or state.last_success_at is None
                or not state.verified_complete
            )
            if matches_ticks or no_fetch_record:
                suspects.append(candle)

        counts = {}
        for candle in suspects:
            counts[candle.symbol] = counts.get(candle.symbol, 0) + 1
        for symbol, count in sorted(counts.items()):
            self.stdout.write(f"{symbol}: {count} suspect adjusted candle(s)")

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: {len(suspects)} suspect row(s); use --apply on a restored copy."
                )
            )
            return

        ids = [row.pk for row in suspects]
        MarketCandle.objects.filter(pk__in=ids).delete()
        now = timezone.now()
        for symbol in counts:
            ArchiveFetchState.objects.update_or_create(
                endpoint=ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
                symbol=symbol,
                defaults={
                    "verified_complete": False,
                    "next_attempt_at": now,
                    "last_error": "Re-armed by audit_adjusted_candles.",
                },
            )
        for symbol, count in sorted(counts.items()):
            remaining = MarketCandle.objects.filter(
                symbol=symbol, timeframe=MarketCandle.ADJUSTED
            ).count()
            self.stdout.write(f"{symbol}: deleted={count}, adjusted_remaining={remaining}")

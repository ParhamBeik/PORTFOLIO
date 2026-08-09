"""Collapse `MarketCandle.date_time` onto the bare Jalali day.

The column holds two shapes: 3,628,480 rows as `1405-05-11` and 5,654 as
`1405-05-11 00:00:00`. Every calendar lookup in the archive is a
`date_time__in=[bare dates]`, so the second shape is simply invisible to them --
the trading calendar, `_symbol_candle_volumes`, and therefore the tick pipeline
all skip those rows.

All 5,654 belong to one symbol (کاما), 32 of them inside the trailing 90-day
window. Where a bare-date twin already exists the long row is a duplicate and is
removed rather than renamed; the rest are renamed in place. Dry-run by default.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Length

from marketdata.models import MarketCandle

TIMESTAMP_SUFFIX = " 00:00:00"


class Command(BaseCommand):
    help = "Normalize MarketCandle.date_time to the bare Jalali day (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        long_rows = MarketCandle.objects.annotate(
            length=Length("date_time")
        ).filter(length=len("1405-05-11") + len(TIMESTAMP_SUFFIX))

        renames, duplicates = [], []
        # A row is a duplicate only when the same symbol+timeframe+day already
        # exists in the bare shape; the unique constraint is on that triple.
        # Scoped to the affected symbols -- the unscoped version pulled all
        # 3.6M bare rows into a set and was killed by the OOM reaper.
        affected = set(long_rows.values_list("symbol", flat=True).distinct())
        existing = set(
            MarketCandle.objects.filter(symbol__in=affected)
            .annotate(length=Length("date_time"))
            .filter(length=10)
            .values_list("symbol", "timeframe", "date_time")
        )
        for pk, symbol, timeframe, date_time in long_rows.values_list(
            "id", "symbol", "timeframe", "date_time"
        ):
            key = (symbol, timeframe, date_time[:10])
            (duplicates if key in existing else renames).append((pk, date_time[:10]))
            existing.add(key)

        symbols = long_rows.values("symbol").annotate(n=Count("id")).order_by("-n")
        self.stdout.write(f"long-form rows : {long_rows.count()}")
        self.stdout.write(f"  rename       : {len(renames)}")
        self.stdout.write(f"  delete as dup: {len(duplicates)}")
        for row in symbols:
            self.stdout.write(f"  {row['symbol']:12} {row['n']}")

        if not options["apply"]:
            self.stdout.write("\nDRY RUN -- nothing written. Re-run with --apply.")
            return

        with transaction.atomic():
            MarketCandle.objects.filter(id__in=[pk for pk, _ in duplicates]).delete()
            for pk, bare in renames:
                MarketCandle.objects.filter(pk=pk).update(date_time=bare)
        self.stdout.write(f"\nrenamed={len(renames)} deleted={len(duplicates)}")

        from marketdata.tasks import _invalidate_returns

        _invalidate_returns()

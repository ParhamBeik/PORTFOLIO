"""Discover older Codal filings in verified Jalali publication-date windows."""

from contextlib import contextmanager
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from marketdata import jalali
from marketdata.codal_history import RequestBudget, record_window_failure, scan_window
from marketdata.fetchers import MarketDataFetchError
from marketdata.models import CodalHistoryWindow, MarketInstrument
from marketdata.quota import QuotaExhausted


def _year_bounds(year):
    start = f"{year:04d}-01-01"
    next_start = jalali.to_gregorian(f"{year + 1:04d}-01-01")
    return start, jalali.from_gregorian(next_start - timedelta(days=1))


@contextmanager
def _exclusive_backfill():
    # One manual run at a time: duplicate scans spend quota without adding
    # coverage. PostgreSQL releases this session lock if the process dies.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [2026092701])
        if not cursor.fetchone()[0]:
            raise CommandError("Another Codal history backfill is running.")
        try:
            yield
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [2026092701])


class Command(BaseCommand):
    help = "Plan or execute quota-bounded historical Codal announcement discovery."

    def add_arguments(self, parser):
        parser.add_argument("--start-year", type=int, required=True)
        parser.add_argument("--end-year", type=int, required=True)
        parser.add_argument(
            "--symbols", help="Comma-separated symbols; default is all eligible TSETMC stocks."
        )
        parser.add_argument("--max-requests", type=int, default=100)
        parser.add_argument("--max-pages", type=int, default=10)
        parser.add_argument(
            "--recheck-after-days", type=int,
            help="Reopen completed windows last verified more than this many days ago.",
        )
        parser.add_argument(
            "--execute", action="store_true",
            help="Create durable windows and spend provider quota. Omit for a read-only plan.",
        )

    def handle(self, *args, **options):
        start_year = options["start_year"]
        end_year = options["end_year"]
        current_year = int(jalali.from_gregorian(timezone.now().astimezone(jalali.TEHRAN))[:4])
        if not 1300 <= start_year <= end_year < current_year:
            raise CommandError("Choose completed Jalali years in ascending order.")
        max_pages = options["max_pages"]
        max_requests = options["max_requests"]
        if not 1 <= max_pages <= 25 or max_requests < max_pages:
            raise CommandError("Use 1–25 max-pages and max-requests >= max-pages.")
        if options["recheck_after_days"] is not None and options["recheck_after_days"] < 1:
            raise CommandError("--recheck-after-days must be positive.")

        if options["symbols"]:
            symbols = sorted({s.strip() for s in options["symbols"].split(",") if s.strip()})
            if not symbols or any(len(s) > 64 for s in symbols):
                raise CommandError("Supply at least one valid symbol (maximum 64 characters).")
        else:
            symbols = list(
                MarketInstrument.objects.filter(
                    source=MarketInstrument.Source.TSETMC,
                    category=MarketInstrument.Category.STOCK,
                    eligible=True,
                ).order_by("symbol").values_list("symbol", flat=True)
            )
        if not symbols:
            raise CommandError("No symbols selected.")

        bounds = [_year_bounds(year) for year in range(start_year, end_year + 1)]
        self.stdout.write(
            f"Codal history: {len(symbols)} symbols × {len(bounds)} completed years; "
            f"at most {max_requests} source requests this run."
        )
        if not options["execute"]:
            self.stdout.write("Read-only plan. Add --execute to create windows and fetch.")
            return

        with _exclusive_backfill():
            self._execute(
                symbols, bounds, max_pages, max_requests, options["recheck_after_days"]
            )

    def _execute(self, symbols, bounds, max_pages, max_requests, recheck_after_days):

        CodalHistoryWindow.objects.bulk_create(
            [
                CodalHistoryWindow(symbol=symbol, date_start=start, date_end=end)
                for start, end in bounds for symbol in symbols
            ],
            ignore_conflicts=True,
            batch_size=500,
        )
        budget = RequestBudget(max_requests)
        lower, upper = bounds[0][0], bounds[-1][1]
        if recheck_after_days is not None:
            stale_before = timezone.now() - timedelta(days=recheck_after_days)
            reopened = CodalHistoryWindow.objects.filter(
                symbol__in=symbols,
                date_start__gte=lower,
                date_end__lte=upper,
                verified_complete=True,
                split=False,
                last_success_at__lt=stale_before,
            ).update(verified_complete=False, next_attempt_at=None)
            self.stdout.write(f"Stale windows reopened: {reopened}.")
        complete = split = failed = 0
        while budget.remaining >= max_pages:
            now = timezone.now()
            window = (
                CodalHistoryWindow.objects.filter(
                    symbol__in=symbols,
                    date_start__gte=lower,
                    date_end__lte=upper,
                    verified_complete=False,
                    split=False,
                ).filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
                .order_by("-date_start", "symbol", "date_end")
                .first()
            )
            if window is None:
                break
            try:
                result = scan_window(window, budget, max_pages=max_pages)
            except QuotaExhausted:
                self.stdout.write("Provider quota paused the historical lane.")
                break
            except MarketDataFetchError as exc:
                record_window_failure(window, exc)
                failed += 1
                continue
            complete += result == "complete"
            split += result == "split"
        self.stdout.write(
            f"Requests attempted: {budget.used}/{budget.limit}; "
            f"windows complete: {complete}; split: {split}; failed: {failed}."
        )

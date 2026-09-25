"""Backfill crypto daily history from Wallex, unmetered.

This closes the largest remaining hole in the warehouse. Before it ran, the
whole crypto surface was two symbols:

    BTC       1,037 rows, 1402-08-09 onward   (~2.8 years)
    USDT_IRT  1,035 rows, 1402-08-09 onward

and nothing at all for ETH, XRP, ADA, DOGE, LTC, BNB, SOL, TRX, DOT or PAXG.
A symbol with no history cannot enter the returns matrix, so it cannot be
optimized over, compared, or risk-scored -- it is invisible to every analytic
in the product regardless of whether a user holds it.

Wallex serves the entire series in ONE request per pair, back to 2018-11-27 for
USDT/Toman and 2019 for BTC, and costs no provider quota. The equivalent
BrsApi-shaped backfill does not exist at any price: its crypto endpoint is live
-only, which is why `portfolio.tasks` had to accumulate this history one day at
a time from the price loop.

    python manage.py backfill_crypto_history --dry-run
    python manage.py backfill_crypto_history
    python manage.py backfill_crypto_history --coins BTC,ETH --quote TMN
    python manage.py backfill_crypto_history --coins SHIB --quote USDT --repair-zero

Safe to re-run: the ingest is insert-only against the `(symbol, date)` unique
constraint, so a second pass adds only genuinely new days.
"""
import time

from django.core.management.base import BaseCommand

from marketdata.ingest import ingest_direct_crypto_history
from marketdata.sources import wallex
from marketdata.sources.http import SourceError

#: Coins worth carrying. The liquid set on the venue plus the two tokenised-gold
#: tokens, which give a Toman-denominated gold series from a completely
#: different mechanism than the coin/bullion feed -- useful precisely because it
#: is independent of it.
DEFAULT_COINS = (
    "BTC", "ETH", "USDT", "XRP", "ADA", "DOGE", "LTC", "BNB", "SOL", "TRX",
    "DOT", "AVAX", "LINK", "MATIC", "SHIB", "UNI", "ATOM", "FIL", "ETC", "BCH",
    "PAXG", "XAUT",
)

#: Quote asset -> (warehouse symbol suffix, declared unit).
#:
#: Both follow conventions the table already uses rather than inventing new
#: ones: `USDT_IRT` is Toman today and `BTC` is tether today. Keeping a Toman
#: series under a distinct symbol is what stops two units sharing one column --
#: the same coin is quoted ~10x apart in Rial and Toman and ~200,000x apart in
#: Toman and tether, and none of those look wrong on a chart.
QUOTES = {
    "TMN": ("_IRT", "تومان"),
    "USDT": ("", "تتر"),
}


class Command(BaseCommand):
    help = "Backfill crypto daily OHLC history from Wallex (unmetered)."

    def add_arguments(self, parser):
        parser.add_argument("--coins", default=",".join(DEFAULT_COINS))
        parser.add_argument(
            "--quote", default="TMN,USDT",
            help="Quote books to fetch. TMN gives a Toman series, USDT a tether one.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Fetch and report what would land, without writing.",
        )
        parser.add_argument(
            "--repair-zero", action="store_true",
            help="Replace only zero OHLC rows for the same symbol and quote unit. "
                 "Valid existing prices remain untouched.",
        )
        parser.add_argument(
            "--sleep", type=float, default=0.4,
            help="Seconds between requests. Wallex publishes no rate limit and "
                 "did not throttle 60 requests in 17s, which is exactly why we "
                 "pace ourselves rather than find their ceiling in production.",
        )

    def handle(self, *args, **options):
        coins = [c.strip().upper() for c in options["coins"].split(",") if c.strip()]
        quotes = [q.strip().upper() for q in options["quote"].split(",") if q.strip()]
        dry = options["dry_run"]

        unknown = [q for q in quotes if q not in QUOTES]
        if unknown:
            self.stderr.write(f"Unknown quote asset(s): {unknown}. Known: {sorted(QUOTES)}")
            return

        # One catalog call decides which pairs exist, so a coin the venue does
        # not list costs nothing instead of a 404 per attempt.
        try:
            available = set(wallex.fetch_markets())
        except SourceError as exc:
            self.stderr.write(f"Wallex unreachable: {exc}")
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\nWallex crypto backfill{' (DRY RUN)' if dry else ''}: "
                f"{len(coins)} coins x {len(quotes)} books"
            )
        )
        self.stdout.write(
            f"{'pair':<12}{'symbol':<12}{'unit':<8}{'candles':>9}{'new':>8}{'known':>8}  range"
        )

        totals = {"created": 0, "known": 0, "pairs": 0, "missing": 0}
        for coin in coins:
            for quote in quotes:
                pair = f"{coin}{quote}"
                if pair not in available:
                    totals["missing"] += 1
                    continue
                suffix, unit = QUOTES[quote]
                symbol = f"{coin}{suffix}"
                try:
                    candles = wallex.candles(wallex.fetch_ohlc(pair, resolution="D"))
                except SourceError as exc:
                    self.stdout.write(self.style.ERROR(f"{pair:<12}{exc}"))
                    continue
                if not candles:
                    self.stdout.write(f"{pair:<12}{symbol:<12}{unit:<8}{0:>9}  no candles")
                    continue

                span = f"{_jalali(candles[0])} .. {_jalali(candles[-1])}"
                if dry:
                    created, known = 0, 0
                else:
                    created, known = ingest_direct_crypto_history(
                        symbol, unit, candles, repair_zero=options["repair_zero"]
                    )
                totals["created"] += created
                totals["known"] += known
                totals["pairs"] += 1
                self.stdout.write(
                    f"{pair:<12}{symbol:<12}{unit:<8}{len(candles):>9}"
                    f"{created:>8}{known:>8}  {span}"
                )
                time.sleep(options["sleep"])

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{totals['pairs']} pairs, {totals['created']} new rows, "
                f"{totals['known']} already present, "
                f"{totals['missing']} pairs not listed on the venue."
            )
        )
        if dry:
            self.stdout.write("Dry run: nothing was written.")


def _jalali(candle):
    from marketdata.jalali import from_epoch

    return from_epoch(candle["ts"])

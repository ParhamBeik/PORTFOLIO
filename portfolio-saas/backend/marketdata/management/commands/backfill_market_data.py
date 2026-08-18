"""Synchronous CLI twin of the marketdata sync tasks (mirrors fetch_prices).

Same fetch+ingest body the Celery tasks run, callable without a worker:
    manage.py backfill_market_data --all --days 365
    manage.py backfill_market_data --symbol کاما --kinds history,candles
Idempotent: the warehouse unique constraints make re-runs free.
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from marketdata import ingest, jalali
from marketdata.fetchers import (
    fetch_candlesticks,
    fetch_codal_announcements,
    fetch_daily_history,
    fetch_gold_currency_pro_history_daily,
    fetch_market_index,
    fetch_shareholders,
    fetch_symbol_data,
    fetch_transactions,
)
from marketdata.tasks import tracked_brs_symbols, tracked_tse_symbols

KINDS = ("metadata", "history", "candles", "codal", "shareholders", "gold", "index", "ticks")
DEFAULT_KINDS = "history,candles,gold,index"


class Command(BaseCommand):
    help = "Backfill the market-data warehouse from BrsApi (synchronous, idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", action="append", default=[],
                            help="TSE symbol to backfill (repeatable). Default: tracked assets.")
        parser.add_argument("--all", action="store_true",
                            help="Backfill every tracked symbol (assets with tse/brs symbols).")
        parser.add_argument("--days", type=int, default=365,
                            help="History window in days. Informational for the history "
                                 "endpoints (BrsApi returns their full series regardless), "
                                 "but real for 'ticks', which costs one request per day.")
        parser.add_argument("--kinds", default=DEFAULT_KINDS,
                            help=f"Comma list from {KINDS}. Default: {DEFAULT_KINDS}. "
                                 "'ticks' (intraday transactions) is only available here, "
                                 "never scheduled — volume is large and nothing consumes it yet.")
        parser.add_argument("--sleep", type=float, default=None,
                            help="Seconds between API calls (default MARKETDATA_FETCH_DELAY).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Fetch but write nothing.")

    def handle(self, *args, **options):
        kinds = [k.strip() for k in options["kinds"].split(",") if k.strip()]
        unknown = [k for k in kinds if k not in KINDS]
        if unknown:
            raise CommandError(f"Unknown kinds {unknown}; choose from {KINDS}.")

        symbols = options["symbol"] or tracked_tse_symbols()
        if not options["symbol"] and not options["all"] and not symbols:
            raise CommandError("No tracked symbols. Pass --symbol or seed assets first.")

        delay = options["sleep"] if options["sleep"] is not None else settings.MARKETDATA_FETCH_DELAY
        tick_days = max(1, options["days"])
        dry = options["dry_run"]
        tse_key = settings.TSETMC_API_KEY
        brs_key = settings.BRS_API_KEY
        totals = {}

        def record(kind, result):
            if dry:
                return
            created, skipped = result
            c, s = totals.get(kind, (0, 0))
            totals[kind] = (c + created, s + skipped)

        def pause():
            time.sleep(delay)

        for symbol in symbols:
            if not tse_key:
                self.stderr.write("TSETMC_API_KEY not set; skipping TSE kinds.")
                break
            if "metadata" in kinds:
                payload = fetch_symbol_data(tse_key, symbol)
                if not dry:
                    record("metadata", ingest.ingest_symbol_metadata(payload))
                pause()
            if "history" in kinds:
                for history_type, adjusted in ((0, False), (1, True)):
                    payload = fetch_daily_history(tse_key, symbol, history_type=history_type)
                    if not dry:
                        record("history", (
                            ingest.ingest_real_legal(symbol, payload)
                            if adjusted else ingest.ingest_daily_history(symbol, payload)
                        ))
                    pause()
            if "candles" in kinds:
                for candle_type in (2, 3):
                    payload = fetch_candlesticks(tse_key, symbol, candle_type=candle_type)
                    if not dry:
                        record("candles", ingest.ingest_candles(symbol, candle_type, payload))
                    pause()
            if "codal" in kinds:
                payload = fetch_codal_announcements(tse_key, symbol=symbol)
                if not dry:
                    record("codal", ingest.ingest_codal(payload))
                pause()
            if "shareholders" in kinds:
                payload = fetch_shareholders(tse_key, symbol)
                if not dry:
                    record("shareholders", ingest.ingest_shareholders(symbol, payload))
                pause()
            if "ticks" in kinds:
                # Ticks are one request per day and the provider requires a Jalali
                # date; with no date it returns [] and every day used to collapse
                # onto the empty-string key.
                for day in jalali.recent_days(tick_days):
                    payload = fetch_transactions(tse_key, symbol, date=day)
                    if not dry:
                        record("ticks", ingest.ingest_transactions(symbol, day, payload))
                    pause()

        if "gold" in kinds:
            if brs_key:
                for brs_symbol in tracked_brs_symbols():
                    payload = fetch_gold_currency_pro_history_daily(brs_key, brs_symbol)
                    if not dry:
                        record("gold", ingest.ingest_gold_currency_history(payload))
                    pause()
            else:
                self.stderr.write("BRS_API_KEY not set; skipping gold.")

        if "index" in kinds and tse_key:
            payload = fetch_market_index(tse_key)
            if not dry:
                record("index", ingest.ingest_market_index(payload))

        if not dry and totals:
            # New history invalidates the analytics returns cache.
            from portfolio.services.returns import invalidate_returns_cache
            invalidate_returns_cache()

        if dry:
            self.stdout.write(self.style.SUCCESS("Dry run: fetched, wrote nothing."))
        else:
            for kind, (created, skipped) in sorted(totals.items()):
                self.stdout.write(f"  {kind}: {created} created, {skipped} skipped")
            self.stdout.write(self.style.SUCCESS("Backfill complete."))

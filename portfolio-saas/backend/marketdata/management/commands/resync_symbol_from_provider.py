"""Rebuild one symbol's price series from what the provider serves today.

For a symbol whose stored history is untrustworthy, arguing row by row is slower
and less certain than asking the provider for the whole series and making the
warehouse match it. The provider is the only authority on its own history, so
"what does it serve now" is the definition of correct.

Found by running it on کاما: 3,239 unadjusted and 3,669 adjusted candle rows sat
on dates the provider has never listed for that symbol, every single one carrying
a volume of exactly 10,000,000. Real volume is never exactly ten million across
thousands of consecutive days -- these were synthetic fill, and they outnumbered
the genuine rows almost one for one.

Three rules, in order:
  * a row on a date the provider does not list is deleted;
  * a row on a date it does list is rewritten to the provider's values;
  * a date the provider lists that we lack is inserted.

Every write goes through `ingest.ingest_candles` / `ingest_daily_history`, so the
usual validation, salvage and rejection recording apply -- this command supplies
data, it does not bypass the gate. Dry-run by default.
"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from marketdata import ingest, market_state
from marketdata.fetchers import fetch_candlesticks, fetch_daily_history
from marketdata.models import DailyStockHistory, MarketCandle

# Two prices agree when within this fraction; only rounding should slip through.
TOLERANCE = 0.01


def _by_date(records, key):
    out = {}
    for record in records or []:
        if isinstance(record, dict) and record.get("date"):
            out[ingest.normalize_jalali(record["date"])[:10]] = record
    return {day: record for day, record in out.items() if record.get(key) is not None}


def _differs(stored, fresh):
    if stored is None or fresh is None:
        return True
    stored, fresh = float(stored), float(fresh)
    if fresh == 0:
        return stored != 0
    return abs(stored - fresh) / abs(fresh) > TOLERANCE


class Command(BaseCommand):
    help = "Make one symbol's candles and daily history match the provider exactly."

    def add_arguments(self, parser):
        parser.add_argument("symbol")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--force-during-session", action="store_true",
            help="Override the closed-market requirement. Rarely correct.",
        )
        parser.add_argument(
            "--skip-history", action="store_true",
            help="Leave DailyStockHistory alone (it is often already correct).",
        )

    def handle(self, *args, **options):
        symbol = options["symbol"]
        key = settings.TSETMC_API_KEY

        # While the session is running the provider serves the day mid-write, and
        # consecutive calls come back different lengths -- 4,506 rows against
        # 4,509 minutes apart. This command deletes whatever the provider does
        # not list, so a mid-session snapshot is the one input it must never act
        # on. Repairs belong after the close.
        if options["apply"] and not options["force_during_session"]:
            if market_state.market_state() == market_state.OPEN:
                raise CommandError(
                    "The TSE session is open and the provider's history shifts "
                    "while it trades. Re-run after the close, or pass "
                    "--force-during-session if you are certain."
                )

        # Fetched twice and required to agree. A flaky call returned 4,506 of
        # 4,509 adjusted rows once, and this command deletes whatever the
        # provider does not list -- so a truncated response is indistinguishable
        # from a shrunken history and would quietly destroy real data. Two
        # requests are cheap; losing years of prices is not.
        history = fetch_daily_history(key, symbol=symbol, history_type=0)
        history_by_date = _by_date(history if isinstance(history, list) else [], "pl")
        adjusted_payload = fetch_candlesticks(key, symbol=symbol, candle_type=3) or {}
        adjusted_by_date = _by_date(
            adjusted_payload.get("candle_daily_adjusted") or [], "close"
        )
        if not history_by_date and not adjusted_by_date:
            self.stdout.write(f"{symbol}: provider served nothing; refusing to touch stored data")
            return

        if options["apply"]:
            confirm_history = fetch_daily_history(key, symbol=symbol, history_type=0)
            confirm_adjusted = fetch_candlesticks(key, symbol=symbol, candle_type=3) or {}
            second = (
                set(_by_date(confirm_history if isinstance(confirm_history, list) else [], "pl")),
                set(_by_date(confirm_adjusted.get("candle_daily_adjusted") or [], "close")),
            )
            if second != (set(history_by_date), set(adjusted_by_date)):
                raise CommandError(
                    f"{symbol}: two consecutive fetches disagree "
                    f"(history {len(history_by_date)} vs {len(second[0])}, "
                    f"adjusted {len(adjusted_by_date)} vs {len(second[1])}). "
                    "One of them is truncated; refusing to delete against it."
                )

        report = {"symbol": symbol}
        report["unadjusted"] = self._plan_candles(
            symbol, MarketCandle.UNADJUSTED, history_by_date, "pl"
        )
        report["adjusted"] = self._plan_candles(
            symbol, MarketCandle.ADJUSTED, adjusted_by_date, "close"
        )
        if not options["skip_history"]:
            report["daily_history"] = self._plan_history(symbol, history_by_date)

        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if not options["apply"]:
            self.stdout.write("\nDRY RUN -- nothing written. Re-run with --apply.")
            return

        with transaction.atomic():
            self._apply_candles(symbol, MarketCandle.UNADJUSTED, history_by_date)
            self._apply_candles(symbol, MarketCandle.ADJUSTED, adjusted_by_date)
            if not options["skip_history"]:
                self._apply_history(symbol, history, history_by_date)

        from marketdata.tasks import _invalidate_returns

        _invalidate_returns()
        self.stdout.write("\napplied.")

    # -- planning -----------------------------------------------------------

    def _plan_candles(self, symbol, timeframe, fresh, value_key):
        stored = {
            day[:10]: close
            for day, close in MarketCandle.objects.filter(
                symbol=symbol, timeframe=timeframe
            ).values_list("date_time", "close_price")
        }
        if not fresh:
            # The provider serves nothing on this timeframe. That is not licence
            # to delete a series it simply does not publish (کاما has no
            # unadjusted candle endpoint at all), so this is reported, not acted on.
            return {"provider_rows": 0, "stored_rows": len(stored), "action": "skipped"}
        phantom = [day for day in stored if day not in fresh]
        rewrite = [
            day for day in stored
            if day in fresh and _differs(stored[day], fresh[day].get(value_key))
        ]
        return {
            "provider_rows": len(fresh),
            "stored_rows": len(stored),
            "delete_not_in_provider": len(phantom),
            "rewrite_value_mismatch": len(rewrite),
            "insert_missing": len(set(fresh) - set(stored)),
            "sample_delete": phantom[:3],
            "sample_rewrite": [
                {"date": day, "stored": float(stored[day]),
                 "provider": float(fresh[day][value_key])}
                for day in rewrite[:3]
            ],
        }

    def _plan_history(self, symbol, fresh):
        stored = {
            day[:10]: pl
            for day, pl in DailyStockHistory.objects.filter(
                symbol=symbol
            ).values_list("date", "pl")
        }
        rewrite = [
            day for day in stored
            if day in fresh and _differs(stored[day], fresh[day].get("pl"))
        ]
        return {
            "provider_rows": len(fresh),
            "stored_rows": len(stored),
            "rewrite_value_mismatch": len(rewrite),
            "insert_missing": len(set(fresh) - set(stored)),
            "sample_rewrite": [
                {"date": day, "stored": float(stored[day] or 0),
                 "provider": float(fresh[day]["pl"])}
                for day in rewrite[:3]
            ],
        }

    # -- applying -----------------------------------------------------------

    def _apply_candles(self, symbol, timeframe, fresh):
        if not fresh:
            return
        value_key = "close" if timeframe == MarketCandle.ADJUSTED else "pl"
        stored = {
            day[:10]: close
            for day, close in MarketCandle.objects.filter(
                symbol=symbol, timeframe=timeframe
            ).values_list("date_time", "close_price")
        }
        # Ingest only *updates* the latest 10 sessions (ingest.py:279) -- settled
        # history is deliberately insert-only, so a rewrite of an old row is
        # silently dropped. A repair has to remove the wrong row first and let
        # the insert put the provider's value in its place.
        wrong = [day for day in stored
                 if day in fresh and _differs(stored[day], fresh[day].get(value_key))]
        MarketCandle.objects.filter(symbol=symbol, timeframe=timeframe).filter(
            date_time__in=wrong
        ).delete()
        MarketCandle.objects.filter(symbol=symbol, timeframe=timeframe).exclude(
            date_time__in=list(fresh)
        ).delete()
        if timeframe == MarketCandle.ADJUSTED:
            payload = {"candle_daily_adjusted": list(fresh.values())}
            ingest.ingest_candles(symbol, 3, payload)
            return
        # No unadjusted candle endpoint exists for every symbol, so the
        # unadjusted series is rebuilt from the history endpoint's own OHLC
        # columns rather than invented.
        ingest.ingest_candles(symbol, 2, {"candle_daily": [
            {
                "date": day,
                "open": record.get("pf"),
                "high": record.get("pmax"),
                "low": record.get("pmin"),
                "close": record.get("pl"),
                "volume": record.get("tvol") or 0,
            }
            for day, record in fresh.items()
        ]})

    def _apply_history(self, symbol, payload, fresh=None):
        if not (isinstance(payload, list) and payload):
            return
        if fresh:
            stored = {
                day[:10]: pl
                for day, pl in DailyStockHistory.objects.filter(
                    symbol=symbol
                ).values_list("date", "pl")
            }
            wrong = [day for day in stored
                     if day in fresh and _differs(stored[day], fresh[day].get("pl"))]
            DailyStockHistory.objects.filter(
                symbol=symbol, date__in=wrong
            ).delete()
        ingest.ingest_daily_history(symbol, payload)

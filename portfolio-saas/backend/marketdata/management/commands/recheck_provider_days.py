"""Ask the provider again before calling stored data wrong.

`audit_warehouse` can see that two stored series disagree, but not which one is
right. Its arbiter is the adjusted candle, and when that agrees with neither the
row is reported and left alone -- 12 such rows today, and the audit is correct to
refuse them.

A third, independent observation settles it: fetch the day again now. If today's
payload matches one stored series, the other is wrong and we know which. If it
matches neither, the provider has changed its own history. If the fresh payload
is still internally inconsistent, the endpoint is at fault and only then is
quarantine the honest answer.

Read-only by default; writes nothing without `--apply`, a backup confirmation and
the manifest's own SHA-256.
"""
import csv
import hashlib
import json
import os
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from marketdata import ingest
from marketdata.fetchers import fetch_candlesticks, fetch_daily_history
from marketdata.models import DailyStockHistory, MarketCandle, RejectedRecord

# Two prices agree when they are within this fraction of each other. The
# disagreements under investigation are 10x, 6.7x and 5x, so the bar only has to
# exclude rounding.
AGREEMENT_TOLERANCE = 0.01

VERDICTS = ("stored_wrong", "provider_changed", "provider_broken", "no_data")


def _close(a, b):
    if a is None or b is None:
        return False
    a, b = float(a), float(b)
    if a == 0 or b == 0:
        return a == b
    return abs(a - b) / max(abs(a), abs(b)) <= AGREEMENT_TOLERANCE


def _fresh_history(symbol):
    """{date: unadjusted close} straight from the provider, ingested nowhere."""
    payload = fetch_daily_history(settings.TSETMC_API_KEY, symbol=symbol, history_type=0)
    if not isinstance(payload, list):
        return {}
    out = {}
    for record in payload:
        if not isinstance(record, dict) or not record.get("date"):
            continue
        out[ingest.normalize_jalali(record["date"])[:10]] = record.get("pl")
    return out


def _fresh_candles(symbol, candle_type):
    payload = fetch_candlesticks(settings.TSETMC_API_KEY, symbol=symbol, candle_type=candle_type)
    if not isinstance(payload, dict):
        return {}
    records = (
        payload.get("candle_daily")
        or payload.get("candle_daily_adjusted")
        or payload.get("candle_intraday")
        or []
    )
    out = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("date"):
            continue
        out[ingest.normalize_jalali(record["date"])[:10]] = record.get("close")
    return out


class Command(BaseCommand):
    help = "Re-fetch disputed days and record which side the provider now supports."

    def add_arguments(self, parser):
        parser.add_argument(
            "--audit-manifest",
            help="warehouse_audit CSV; disputed rows are taken from its "
                 "crosstable findings. Omit and pass --symbol to check a symbol whole.",
        )
        parser.add_argument("--symbol", action="append", default=[])
        parser.add_argument("--manifest-path", default="provider_recheck.csv")
        parser.add_argument("--limit", type=int, default=200,
                            help="Cap on disputed days examined; requests are per symbol, not per day.")

    def handle(self, *args, **options):
        disputes = self._disputes(options)
        if not disputes:
            raise CommandError("Nothing to recheck: pass --audit-manifest and/or --symbol.")

        rows = []
        for symbol, dates in sorted(disputes.items()):
            self.stdout.write(f"[{symbol}] re-fetching ({len(dates)} disputed day(s))")
            rows.extend(self._recheck_symbol(symbol, sorted(dates)[: options["limit"]]))

        path = options["manifest_path"]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "symbol", "date", "verdict", "stored_candle", "stored_history",
                "fresh_candle", "fresh_history", "corrected_table", "corrected_value",
                "evidence",
            ])
            writer.writeheader()
            writer.writerows(rows)

        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        counts = defaultdict(int)
        for row in rows:
            counts[row["verdict"]] += 1
        self.stdout.write("\n=== verdicts ===")
        for verdict in VERDICTS:
            self.stdout.write(f"  {verdict:18} {counts[verdict]}")
        self.stdout.write(f"\nmanifest : {path}\nsha256   : {digest}")
        self.stdout.write(
            "\nREAD-ONLY. Repairs go through repair_warehouse with this digest, "
            "after a backup and explicit approval."
        )
        return json.dumps(dict(counts))

    def _disputes(self, options):
        """Disputed (symbol, date) pairs, from an audit manifest and/or --symbol."""
        disputes = defaultdict(set)
        manifest = options.get("audit_manifest")
        if manifest:
            with open(manifest, encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if row["check"] == "crosstable" and row["symbol"] and row["date"]:
                        disputes[row["symbol"]].add(row["date"][:10])
        for symbol in options["symbol"]:
            # No dates given: compare every day the two tables both hold.
            stored = set(
                MarketCandle.objects.filter(symbol=symbol, timeframe=MarketCandle.UNADJUSTED)
                .values_list("date_time", flat=True)
            )
            history = set(
                DailyStockHistory.objects.filter(symbol=symbol, is_adjusted=False)
                .values_list("date", flat=True)
            )
            disputes[symbol].update(
                {day[:10] for day in stored} & {day[:10] for day in history}
            )
        return disputes

    def _recheck_symbol(self, symbol, dates):
        """One provider request per endpoint, then judge every disputed day."""
        fresh_history = _fresh_history(symbol)
        fresh_unadjusted = _fresh_candles(symbol, candle_type=2)

        stored_candles = {
            day[:10]: value
            for day, value in MarketCandle.objects.filter(
                symbol=symbol, timeframe=MarketCandle.UNADJUSTED
            ).values_list("date_time", "close_price")
        }
        stored_history = {
            day[:10]: value
            for day, value in DailyStockHistory.objects.filter(
                symbol=symbol, is_adjusted=False
            ).values_list("date", "pl")
        }

        rows = []
        for date in dates:
            rows.append(self._judge(
                symbol, date,
                stored_candles.get(date), stored_history.get(date),
                fresh_unadjusted.get(date), fresh_history.get(date),
            ))
        return rows

    def _judge(self, symbol, date, stored_candle, stored_history, fresh_candle, fresh_history):
        row = {
            "symbol": symbol, "date": date,
            "stored_candle": stored_candle, "stored_history": stored_history,
            "fresh_candle": fresh_candle, "fresh_history": fresh_history,
            "corrected_table": "", "corrected_value": "",
        }
        if fresh_candle is None and fresh_history is None:
            row["verdict"] = "no_data"
            row["evidence"] = "provider no longer serves this day on either endpoint"
            return row

        # The provider disagreeing with itself today is the endpoint's fault, not
        # the warehouse's. Nothing here is safe to copy in.
        if (
            fresh_candle is not None and fresh_history is not None
            and not _close(fresh_candle, fresh_history)
        ):
            row["verdict"] = "provider_broken"
            row["evidence"] = (
                f"fresh candle {fresh_candle} vs fresh history {fresh_history} still "
                "disagree; the endpoint contradicts itself, quarantine is the only "
                "honest option"
            )
            return row

        fresh = fresh_candle if fresh_candle is not None else fresh_history
        candle_ok = _close(stored_candle, fresh)
        history_ok = _close(stored_history, fresh)

        if candle_ok and not history_ok:
            row["verdict"] = "stored_wrong"
            row["corrected_table"] = "marketdata_dailystockhistory"
            row["corrected_value"] = fresh
            row["evidence"] = (
                f"provider now says {fresh}; the stored candle agrees, the stored "
                f"history row {stored_history} does not"
            )
        elif history_ok and not candle_ok:
            row["verdict"] = "stored_wrong"
            row["corrected_table"] = "marketdata_marketcandle"
            row["corrected_value"] = fresh
            row["evidence"] = (
                f"provider now says {fresh}; the stored history agrees, the stored "
                f"candle {stored_candle} does not"
            )
        elif candle_ok and history_ok:
            row["verdict"] = "no_data"
            row["evidence"] = f"both stored series already agree with the provider's {fresh}"
        else:
            row["verdict"] = "provider_changed"
            row["corrected_value"] = fresh
            row["evidence"] = (
                f"provider now says {fresh}, matching neither the stored candle "
                f"{stored_candle} nor the stored history {stored_history}"
            )
        return row

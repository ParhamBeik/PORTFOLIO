"""Apply repairs from an `audit_warehouse` manifest, locked to its SHA-256.

Dry-run by default. `--apply` additionally requires `--manifest-hash`, which is
re-computed from the file and must match: the rows that were reviewed are then
provably the rows that get written. Same two-phase idiom as
`backfill_validation`, for the same reason -- these writes are irreversible.

Batches are separate so each can be reviewed and applied on its own:

  quarantine   RejectedRecord markers only. Reversible, and the filters in
               returns.py / valuation.py already honour them, so bad rows stop
               affecting numbers without any price being edited.
  candletable  Delete BRS (Toman) rows sitting in the Rial MarketCandle table.
               No data is lost -- GoldCurrencyHistory is the series of record.
  units        Rewrite mis-scaled closes to the manifest's `corrected` value.
               `--symbol` / `--date-from` / `--date-to` narrow it further, so a
               verified cluster can be repaired without touching unverified rows.
"""
import csv
import hashlib
from collections import Counter
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from marketdata.models import MarketCandle, RejectedRecord

BATCHES = ("quarantine", "candletable", "units")
TIMEFRAME_OF = {
    "marketdata_marketcandle[1d_adj]": MarketCandle.ADJUSTED,
    "marketdata_marketcandle[1d_unadj]": MarketCandle.UNADJUSTED,
}


class Command(BaseCommand):
    help = "Apply repairs from an audit_warehouse manifest (hash-locked)."

    def add_arguments(self, parser):
        parser.add_argument("--manifest-path", required=True)
        parser.add_argument("--manifest-hash")
        parser.add_argument("--batch", required=True, choices=BATCHES)
        parser.add_argument("--symbol", action="append")
        parser.add_argument(
            "--exclude-symbol", action="append",
            help="Skip these symbols (e.g. one awaiting provider verification).",
        )
        parser.add_argument("--date-from")
        parser.add_argument("--date-to")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **o):
        path, expected, batch = o["manifest_path"], o["manifest_hash"], o["batch"]
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if o["apply"]:
            if not expected:
                raise CommandError("--manifest-hash is required with --apply.")
            if digest != expected:
                raise CommandError(
                    f"Manifest hash mismatch.\n  expected {expected}\n  actual   {digest}\n"
                    "The file changed since it was reviewed; re-run audit_warehouse."
                )
        self.stdout.write(f"manifest sha256: {digest}")

        with open(path, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r["verdict"] == "unit_error"]

        if batch == "candletable":
            rows = [r for r in rows if r["check"] == "candletable"]
        elif batch == "units":
            # Any finding that carries a `corrected` value is repairable, whoever
            # produced it -- the units check or the cross-table arbiter.
            rows = [r for r in rows if r.get("corrected")]
        if o["symbol"]:
            rows = [r for r in rows if r["symbol"] in set(o["symbol"])]
        if o["exclude_symbol"]:
            rows = [r for r in rows if r["symbol"] not in set(o["exclude_symbol"])]
        if o["date_from"]:
            rows = [r for r in rows if r["date"] >= o["date_from"]]
        if o["date_to"]:
            rows = [r for r in rows if r["date"] <= o["date_to"]]

        if not rows:
            self.stdout.write("No matching rows. Nothing to do.")
            return

        self.stdout.write(f"batch={batch}  matched {len(rows)} row(s)")
        for sym, n in Counter(r["symbol"] for r in rows).most_common(10):
            self.stdout.write(f"    {sym:16} {n}")
        for r in rows[:5]:
            self.stdout.write(
                f"  e.g. {r['symbol']} {r['date']} {r['table']}: "
                f"{r['value']} -> {r.get('corrected') or 'DELETE'}"
            )

        if not o["apply"]:
            self.stdout.write("\nDRY RUN -- nothing written. Re-run with --apply "
                              f"--manifest-hash {digest}")
            return

        handler = {
            "quarantine": self._quarantine,
            "candletable": self._delete_rows,
            "units": self._rewrite,
        }[batch]
        with transaction.atomic():
            changed = handler(rows)
        self.stdout.write(self.style.SUCCESS(f"Applied: {changed} row(s) affected."))
        from marketdata.tasks import _invalidate_returns
        _invalidate_returns()
        self.stdout.write("Returns cache invalidated.")

    def _quarantine(self, rows):
        n = 0
        for r in rows:
            _, created = RejectedRecord.objects.get_or_create(
                endpoint=f"series:{r['table'].split('[')[-1].rstrip(']')}"
                         if "[" in r["table"] else r["check"],
                # RejectedRecord.date is CharField(10); warehouse date_time can
                # carry a " 00:00:00" suffix, so bare-date it here too.
                symbol=r["symbol"], date=r["date"][:10], reason="unit_error",
                defaults={"payload": {"value": r["value"],
                                      "corrected": r.get("corrected", ""),
                                      "evidence": r["evidence"]}},
            )
            n += int(created)
        return n

    def _delete_rows(self, rows):
        n = 0
        for r in rows:
            n += MarketCandle.objects.filter(
                symbol=r["symbol"], date_time__startswith=r["date"]
            ).delete()[0]
        return n

    def _rewrite(self, rows):
        n = 0
        for r in rows:
            tf = TIMEFRAME_OF.get(r["table"])
            if not tf:
                continue
            n += MarketCandle.objects.filter(
                symbol=r["symbol"], timeframe=tf, date_time__startswith=r["date"]
            ).update(close_price=Decimal(r["corrected"]))
        return n

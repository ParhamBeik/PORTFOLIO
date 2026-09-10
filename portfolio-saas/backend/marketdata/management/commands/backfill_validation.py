import csv
import hashlib
import os
import tempfile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from marketdata.tasks import nightly_series_validation
from marketdata.models import CorporateAction, RejectedRecord, MarketCandle, GoldCurrencyHistory
from decimal import Decimal

class Command(BaseCommand):
    help = "Transactional, manifest-locked historical backfill for F2/F3 validation (CorporateActions and RejectedRecords)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--symbols",
            type=str,
            help="Comma-separated TSE symbols to validate/backfill",
        )
        parser.add_argument(
            "--gold-symbols",
            type=str,
            help="Comma-separated Gold/Currency symbols to validate/backfill",
        )
        parser.add_argument(
            "--start-date",
            type=str,
            help="Jalali start date (YYYY-MM-DD)",
        )
        parser.add_argument(
            "--end-date",
            type=str,
            help="Jalali end date (YYYY-MM-DD)",
        )
        parser.add_argument(
            "--manifest-path",
            type=str,
            help="Path to save dry-run CSV manifest or read from during apply",
        )
        parser.add_argument(
            "--manifest-hash",
            type=str,
            help="Expected SHA-256 hash of the manifest file for verification",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually apply changes from the manifest to the database",
        )

    def handle(self, *args, **options):
        apply_mode = options["apply"]
        manifest_path = options["manifest_path"]
        manifest_hash = options["manifest_hash"]

        if apply_mode:
            if not manifest_path or not manifest_hash:
                raise CommandError("Both --manifest-path and --manifest-hash are required when --apply is set.")
            self.apply_backfill(manifest_path, manifest_hash)
        else:
            self.run_dry_run(options)

    def run_dry_run(self, options):
        # 1. Parse filter limits
        symbols_opt = options["symbols"]
        gold_symbols_opt = options["gold_symbols"]
        start_date = options["start_date"]
        end_date = options["end_date"]

        # TSE symbols list
        if symbols_opt:
            tse_symbols = [s.strip() for s in symbols_opt.split(",") if s.strip()]
        else:
            tse_symbols = list(
                MarketCandle.objects.filter(timeframe=MarketCandle.ADJUSTED)
                .order_by()
                .values_list("symbol", flat=True)
                .distinct()
            )

        # Gold/currency symbols list
        if gold_symbols_opt:
            gold_symbols = [s.strip() for s in gold_symbols_opt.split(",") if s.strip()]
        else:
            gold_symbols = list(
                GoldCurrencyHistory.objects.order_by()
                .values_list("symbol", flat=True)
                .distinct()
            )

        # Filter by date ranges
        self.stdout.write(self.style.NOTICE("=== BACKFILL DRY-RUN ==="))
        self.stdout.write(f"TSE Symbols count: {len(tse_symbols)}")
        self.stdout.write(f"Gold/Currency Symbols count: {len(gold_symbols)}")
        if start_date or end_date:
            self.stdout.write(f"Date limit: {start_date or 'MIN'} to {end_date or 'MAX'}")

        # Run validation in dry-run mode
        results = nightly_series_validation(
            dry_run=True,
            symbols=tse_symbols,
            gold_symbols=gold_symbols
        )

        # Filter candidates and rejections by start/end date
        corporate_candidates = results["corporate_action_candidates"]
        spikes = results["proposed_rejections"]

        if start_date:
            corporate_candidates = [c for c in corporate_candidates if c["date"] >= start_date]
            spikes = [s for s in spikes if s["date"] >= start_date]
        if end_date:
            corporate_candidates = [c for c in corporate_candidates if c["date"] <= end_date]
            spikes = [s for s in spikes if s["date"] <= end_date]

        # Count confirmed vs unconfirmed corporate actions
        confirmed_actions = [c for c in corporate_candidates if c["status"] == "confirmed"]
        unconfirmed_candidates = [c for c in corporate_candidates if c["status"] == "unconfirmed"]

        # Write manifest if path provided
        manifest_file_path = options["manifest_path"]
        if not manifest_file_path:
            fd, manifest_file_path = tempfile.mkstemp(
                prefix="f2_f3_backfill_manifest_", suffix=".csv"
            )
            os.close(fd)
        self.stdout.write(f"Writing manifest to: {manifest_file_path}")

        normal_count = 0
        with open(manifest_file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["type", "symbol", "date", "value", "confirmed", "details"])

            # 1. Confirmed corporate actions
            for c in confirmed_actions:
                writer.writerow([
                    "confirmed_corporate_action",
                    c["symbol"],
                    c["date"],
                    str(c["factor"]),
                    "True",
                    c["reason"]
                ])

            # 2. Unconfirmed candidates
            for c in unconfirmed_candidates:
                writer.writerow([
                    "unconfirmed_candidate",
                    c["symbol"],
                    c["date"],
                    str(c["factor"]),
                    "False",
                    c["reason"]
                ])

            # 3. Confirmed spikes
            for s in spikes:
                writer.writerow([
                    "confirmed_series_spike",
                    s["symbol"],
                    s["date"],
                    str(s["payload"].get("close", 0.0)),
                    "True",
                    f"Spike detected. previous_close={s['payload'].get('previous_close')}, log_return={s['payload'].get('log_return'):.4f}"
                ])

            # 4. Normal observations
            action_dates = {c["date"] for c in corporate_candidates}
            spike_dates = {s["date"] for s in spikes}
            for symbol in tse_symbols:
                dates = list(MarketCandle.objects.filter(symbol=symbol, timeframe=MarketCandle.ADJUSTED).order_by("date_time").values_list("date_time", "close_price"))
                for dt, close in dates:
                    date = dt.split()[0]
                    if (start_date and date < start_date) or (end_date and date > end_date):
                        continue
                    if date not in action_dates and date not in spike_dates:
                        writer.writerow(["normal_observation", symbol, date, str(close), "True", "Normal price observation"])
                        normal_count += 1

            for symbol in gold_symbols:
                dates = list(GoldCurrencyHistory.objects.filter(symbol=symbol).order_by("date").values_list("date", "close_price"))
                for dt, close in dates:
                    date = dt.split()[0]
                    if (start_date and date < start_date) or (end_date and date > end_date):
                        continue
                    if date not in spike_dates:
                        writer.writerow(["normal_observation", symbol, date, str(close), "True", "Normal price observation"])
                        normal_count += 1

        # Compute SHA-256 of the manifest
        sha256 = hashlib.sha256()
        with open(manifest_file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256.update(chunk)
        manifest_sha = sha256.hexdigest()

        self.stdout.write(self.style.SUCCESS(f"\nDry-run completed successfully!"))
        self.stdout.write(f"Proposed Confirmed Corporate Actions: {len(confirmed_actions)}")
        self.stdout.write(f"Proposed Unconfirmed Candidates: {len(unconfirmed_candidates)}")
        self.stdout.write(f"Proposed Series Spikes: {len(spikes)}")
        self.stdout.write(f"Normal Observations: {normal_count}")
        self.stdout.write(f"Manifest SHA-256: {manifest_sha}")
        self.stdout.write(self.style.WARNING("To apply these changes, run the command with --apply, --manifest-path, and --manifest-hash options."))

    def apply_backfill(self, manifest_path, manifest_hash):
        # 1. Compute SHA-256 of the file and verify it matches the hash
        sha256 = hashlib.sha256()
        try:
            with open(manifest_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    sha256.update(chunk)
        except FileNotFoundError:
            raise CommandError(f"Manifest file not found at: {manifest_path}")

        computed_sha = sha256.hexdigest()
        if computed_sha != manifest_hash:
            raise CommandError(
                f"Manifest integrity verification failed!\n"
                f"  Expected: {manifest_hash}\n"
                f"  Computed: {computed_sha}"
            )

        self.stdout.write(self.style.NOTICE("Manifest integrity verified successfully!"))
        self.stdout.write(f"Reading manifest: {manifest_path}")

        # Parse CSV inside atomic transaction
        actions_created = 0
        rejections_created = 0

        # Map symbol asset class to correct BRS endpoints (F3)
        def get_gold_currency_asset_class(symbol):
            if symbol in {"BTC", "USDT_IRT"}:
                return "crypto"
            if symbol in {"XAUUSD"}:
                return "commodity"
            if symbol.startswith("IR_GOLD_") or symbol.startswith("IR_COIN_"):
                return "gold"
            return "currency"

        ASSET_CLASS_ENDPOINTS = {
            "crypto": "crypto_daily",
            "commodity": "commodity_daily",
            "gold": "gold_daily",
            "currency": "gold_daily",
        }

        with transaction.atomic():
            with open(manifest_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row_type = row["type"]
                    symbol = row["symbol"]
                    date = row["date"]
                    value = row["value"]

                    if row_type == "confirmed_corporate_action":
                        details = row["details"].lower()
                        kind = CorporateAction.Kind.CAPITAL_INCREASE
                        if "assembly_decision" in details:
                            kind = CorporateAction.Kind.DIVIDEND

                        _, created = CorporateAction.objects.update_or_create(
                            symbol=symbol,
                            date=date,
                            defaults={
                                "factor": Decimal(value),
                                "kind": kind,
                                "source": CorporateAction.Source.CODAL,
                            }
                        )
                        actions_created += int(created)

                    elif row_type == "confirmed_series_spike":
                        is_stock = MarketCandle.objects.filter(symbol=symbol).exists()
                        if is_stock:
                            endpoint = "series:1d_adj"
                        else:
                            asset_class = get_gold_currency_asset_class(symbol)
                            endpoint = ASSET_CLASS_ENDPOINTS[asset_class]

                        try:
                            payload = {"symbol": symbol, "kind": endpoint, "date": date, "close": float(value)}
                        except ValueError:
                            payload = {"symbol": symbol, "kind": endpoint, "date": date}

                        _, created = RejectedRecord.objects.get_or_create(
                            endpoint=endpoint,
                            symbol=symbol,
                            date=date,
                            reason="series_spike",
                            defaults={"payload": payload, "occurrences": 1}
                        )
                        rejections_created += int(created)

        self.stdout.write(self.style.SUCCESS("\nBackfill execution completed!"))
        self.stdout.write(f"Corporate Actions Created/Updated: {actions_created}")
        self.stdout.write(f"Spike Rejections Created: {rejections_created}")
        self.stdout.write(f"Total database changes: {actions_created + rejections_created}")

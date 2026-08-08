"""Build or apply a SHA-locked, idempotent rejected-record recovery manifest."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from marketdata.models import (
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    RealLegalHistory,
    RejectedRecord,
)


REAL_LEGAL_FIELDS = {
    "Buy_CountI": "buy_count_i", "Buy_CountN": "buy_count_n",
    "Sell_CountI": "sell_count_i", "Sell_CountN": "sell_count_n",
    "Buy_I_Volume": "buy_i_volume", "Buy_N_Volume": "buy_n_volume",
    "Sell_I_Volume": "sell_i_volume", "Sell_N_Volume": "sell_n_volume",
    "Buy_I_Value": "buy_i_value", "Buy_N_Value": "buy_n_value",
    "Sell_I_Value": "sell_i_value", "Sell_N_Value": "sell_n_value",
}


def _real_legal_action(row, existing=None):
    payload = row.payload if isinstance(row.payload, dict) else {}
    buy = int(payload.get("Buy_I_Volume") or 0) + int(payload.get("Buy_N_Volume") or 0)
    sell = int(payload.get("Sell_I_Volume") or 0) + int(payload.get("Sell_N_Volume") or 0)
    mismatch = abs(buy - sell) / max(buy, sell, 1)
    exists = (row.symbol, row.date) in existing if existing is not None else RealLegalHistory.objects.filter(symbol=row.symbol, date=row.date).exists()
    if exists:
        return "salvaged_evidence", mismatch, "RealLegalHistory"
    if row.date and buy > 0 and sell > 0 and mismatch <= 0.01:
        return "recoverable", mismatch, "RealLegalHistory"
    return "quarantined", mismatch, ""


def _field_exists(row, existing=None):
    if not row.date or not row.symbol:
        return False
    if existing is not None:
        return (row.symbol, row.date) in existing
    if row.endpoint.startswith("stock_history"):
        return DailyStockHistory.objects.filter(symbol=row.symbol, date=row.date).exists()
    if row.endpoint.startswith("series:") or row.endpoint.startswith("stock_candle"):
        timeframe = MarketCandle.ADJUSTED if "adj" in row.endpoint and "unadj" not in row.endpoint else MarketCandle.UNADJUSTED
        return MarketCandle.objects.filter(symbol=row.symbol, timeframe=timeframe, date_time=row.date).exists()
    if row.endpoint == "gold_daily":
        return GoldCurrencyHistory.objects.filter(symbol=row.symbol, date=row.date).exists()
    return False


def _gold_action(row, existing=None):
    if _field_exists(row, existing):
        return "salvaged_evidence", None, "GoldCurrencyHistory"
    payload = row.payload if isinstance(row.payload, dict) else {}
    close = payload.get("close")
    if close is None and isinstance(payload.get("record"), dict):
        close = payload["record"].get("close")
    try:
        close = Decimal(str(close))
    except Exception:
        return "quarantined", None, ""
    if not row.date or not row.symbol or close <= 0:
        return "quarantined", None, ""
    base = GoldCurrencyHistory.objects.filter(symbol=row.symbol, close_price__gt=0)
    neighbours = [
        *base.filter(date__lt=row.date).order_by("-date").values_list("close_price", flat=True)[:5],
        *base.filter(date__gt=row.date).order_by("date").values_list("close_price", flat=True)[:5],
    ]
    if not neighbours:
        return "quarantined", None, ""
    ordered = sorted(Decimal(value) for value in neighbours)
    median = ordered[len(ordered) // 2]
    error = abs(close - median) / median if median else Decimal("1")
    return ("recoverable", error, "GoldCurrencyHistory") if error <= Decimal("0.50") else ("quarantined", error, "")


def classify(row, existing=None):
    if row.endpoint == "real_legal_history" and row.reason == "buy_sell_volume_mismatch":
        return _real_legal_action(row, None if existing is None else existing["real"])
    if row.reason.startswith("field_"):
        group = "gold" if row.endpoint == "gold_daily" else "daily" if row.endpoint.startswith("stock_history") else "candle"
        return ("salvaged_evidence", None, "") if _field_exists(row, None if existing is None else existing[group]) else ("quarantined", None, "")
    if row.reason == "series_spike" or row.endpoint.startswith("series:"):
        return "anomaly_evidence", None, ""
    if row.endpoint == "gold_daily":
        return _gold_action(row, None if existing is None else existing["gold"])
    return "quarantined", None, ""


def build_manifest():
    rows = list(RejectedRecord.objects.order_by("pk"))

    def existing_pairs(model, candidates, date_field="date", **filters):
        by_symbol = {}
        for row in candidates:
            if row.symbol and row.date:
                by_symbol.setdefault(row.symbol, set()).add(row.date)
        pairs = set()
        for symbol, dates in by_symbol.items():
            query = model.objects.filter(symbol=symbol, **{f"{date_field}__in": dates}, **filters)
            pairs.update(query.values_list("symbol", date_field))
        return pairs

    existing = {
        "real": existing_pairs(RealLegalHistory, [row for row in rows if row.endpoint == "real_legal_history"]),
        "daily": existing_pairs(DailyStockHistory, [row for row in rows if row.endpoint.startswith("stock_history")]),
        "candle": existing_pairs(MarketCandle, [row for row in rows if row.endpoint.startswith("series:") or row.endpoint.startswith("stock_candle")], date_field="date_time"),
        "gold": existing_pairs(GoldCurrencyHistory, [row for row in rows if row.endpoint == "gold_daily"]),
    }
    actions = []
    for row in rows:
        disposition, error, destination = classify(row, existing)
        actions.append({
            "rejected_record_id": row.pk,
            "disposition": disposition,
            "reconciliation_error": str(error) if error is not None else None,
            "destination": destination,
        })
    return {"version": 1, "generated_at": timezone.now().isoformat(), "actions": actions}


def _canonical(manifest):
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class Command(BaseCommand):
    help = "Dry-run by default; --apply requires a reviewed SHA-locked manifest and backup confirmation."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--backup-confirmed", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["manifest"])
        checksum_path = Path(f"{path}.sha256")
        if not options["apply"]:
            manifest = build_manifest()
            payload = _canonical(manifest)
            path.write_bytes(payload)
            checksum = hashlib.sha256(payload).hexdigest()
            checksum_path.write_text(f"{checksum}  {path.name}\n")
            counts = {}
            for action in manifest["actions"]:
                counts[action["disposition"]] = counts.get(action["disposition"], 0) + 1
            self.stdout.write(json.dumps({"manifest": str(path), "sha256": checksum, "counts": counts}, sort_keys=True))
            return

        if not options["backup_confirmed"]:
            raise CommandError("--backup-confirmed is required before database writes")
        if not path.exists() or not checksum_path.exists():
            raise CommandError("manifest and .sha256 file are required")
        payload = path.read_bytes()
        expected = checksum_path.read_text().split()[0]
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise CommandError("manifest checksum mismatch")
        manifest = json.loads(payload)
        recovered = 0
        with transaction.atomic():
            for action in manifest["actions"]:
                row = RejectedRecord.objects.select_for_update().get(pk=action["rejected_record_id"])
                disposition = action["disposition"]
                destination_reference = ""
                if disposition == "recoverable" and action["destination"] == "RealLegalHistory":
                    values = {target: row.payload.get(source) for source, target in REAL_LEGAL_FIELDS.items()}
                    mismatch = Decimal(action["reconciliation_error"])
                    record, _ = RealLegalHistory.objects.get_or_create(
                        symbol=row.symbol,
                        date=row.date,
                        defaults={**values, "quality": "degraded", "reconciliation_error": mismatch},
                    )
                    disposition = "recovered"
                    destination_reference = f"RealLegalHistory:{record.pk}"
                    recovered += 1
                elif disposition == "recoverable" and action["destination"] == "GoldCurrencyHistory":
                    close = Decimal(str(row.payload.get("close")))
                    record, _ = GoldCurrencyHistory.objects.get_or_create(
                        symbol=row.symbol,
                        date=row.date,
                        defaults={"close_price": close, "unit": "تومان"},
                    )
                    disposition = "recovered"
                    destination_reference = f"GoldCurrencyHistory:{record.pk}"
                    recovered += 1
                row.disposition = disposition
                row.destination_reference = destination_reference or action["destination"]
                row.recovered_at = timezone.now() if disposition == "recovered" else None
                row.save(update_fields=["disposition", "destination_reference", "recovered_at"])
        self.stdout.write(json.dumps({"recovered": recovered, "manifest_sha256": actual}, sort_keys=True))

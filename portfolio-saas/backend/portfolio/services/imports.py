"""Validated, atomic and idempotent CSV imports for account ledgers."""
import csv
import hashlib
import io

from django.db import transaction
from django.utils.dateparse import parse_datetime

from ..models import ImportBatch, LedgerEntry
from .catalog import resolve_asset_key
from .ledger import LedgerError, create_ledger_entry


CSV_FIELDS = (
    "external_id", "occurred_at", "kind", "asset_key", "quantity",
    "unit_price_tomans", "amount_tomans", "note",
)
MAX_CSV_BYTES = 2 * 1024 * 1024


class LedgerImportError(Exception):
    def __init__(self, detail: str, *, row: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.row = row


def read_csv_upload(upload) -> tuple[bytes, list[dict]]:
    if not upload or not upload.name.lower().endswith(".csv"):
        raise LedgerImportError("A .csv file is required.")
    raw = upload.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise LedgerImportError("CSV file exceeds the 2 MB limit.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LedgerImportError("CSV must be UTF-8 encoded.") from None
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != CSV_FIELDS:
        raise LedgerImportError("CSV headers do not match the required ledger schema.")
    rows = list(reader)
    if not rows:
        raise LedgerImportError("CSV contains no data rows.")
    return raw, rows


def _asset(row: dict, row_number: int, user):
    key = (row.get("asset_key") or "").strip()
    if not key:
        return None
    # Scoped to the importing user, like every other asset_key entry point --
    # a CSV is request-supplied data exactly as a JSON body is.
    asset = resolve_asset_key(user, key)
    if asset is None:
        raise LedgerImportError("Unknown asset_key.", row=row_number)
    return asset


def _occurred_at(row: dict, row_number: int):
    value = parse_datetime((row.get("occurred_at") or "").strip())
    if value is None:
        raise LedgerImportError("occurred_at must be an ISO-8601 datetime.", row=row_number)
    return value


def _create_rows(account, rows: list[dict], *, batch=None) -> None:
    # A historical export is not necessarily chronological, and the ledger
    # rejects a sell before its buy. Sort by event time first (stable, so
    # same-instant rows keep file order), but report the caller's original
    # line numbers so an error points at the row they can actually see.
    numbered = [
        (index, row, _occurred_at(row, index))
        for index, row in enumerate(rows, start=1)
    ]
    numbered.sort(key=lambda item: (item[2], item[0]))

    for row_number, row, occurred_at in numbered:
        external_id = (row.get("external_id") or "").strip()
        if external_id and LedgerEntry.objects.filter(
            account=account, external_id=external_id
        ).exists():
            raise LedgerImportError("external_id already exists.", row=row_number)
        try:
            create_ledger_entry(
                account=account,
                kind=(row.get("kind") or "").strip(),
                asset=_asset(row, row_number, account.user),
                quantity=(row.get("quantity") or "").strip() or None,
                unit_price_tomans=(row.get("unit_price_tomans") or "").strip() or None,
                amount_tomans=(row.get("amount_tomans") or "").strip() or None,
                occurred_at=occurred_at,
                source="csv",
                note=(row.get("note") or "").strip(),
                external_id=external_id,
                import_batch=batch,
            )
        except (LedgerError, TypeError) as exc:
            raise LedgerImportError(str(exc), row=row_number) from None


@transaction.atomic
def preview_ledger_import(account, upload) -> dict:
    _raw, rows = read_csv_upload(upload)
    _create_rows(account, rows)
    transaction.set_rollback(True)
    return {"valid": True, "row_count": len(rows)}


@transaction.atomic
def commit_ledger_import(account, upload) -> tuple[ImportBatch, bool]:
    raw, rows = read_csv_upload(upload)
    digest = hashlib.sha256(raw).hexdigest()
    existing = ImportBatch.objects.filter(account=account, file_hash=digest).first()
    if existing:
        return existing, False
    batch = ImportBatch.objects.create(
        account=account, file_hash=digest, row_count=len(rows)
    )
    _create_rows(account, rows, batch=batch)
    return batch, True

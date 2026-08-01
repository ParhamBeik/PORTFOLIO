"""Purge rows the old ingest stored wrongly, and re-arm the states that wrote them.

Each deletion below corresponds to a defect fixed in this change set. The rows
cannot be repaired in place -- they are either all-zero, keyed on an empty date,
or keyed on Persian-Indic digits -- and in every case they occupy a unique-key
slot that would stop the corrected fetch from ever landing.

Deleting is only half the job: a state left `verified_complete` is deferred to
the next post-close window and never re-examined, so the affected states are
reset to "never attempted". That puts them in `claim_archive_batch`'s
starvation reserve, which is exactly the queue meant for work nobody has done.

Not reversible by design: the deleted rows carry no information. The reverse
operation only re-arms the states, so a rollback still re-fetches.
"""
from django.db import migrations

# The archive states whose stored rows this migration removes. Everything else
# (candles, unadjusted history) verified honestly and is left alone -- re-running
# those would spend ~1,900 requests re-confirming data the audit found clean.
AFFECTED_ENDPOINTS = [
    "stock_history_adjusted",   # was the Real/Legal breakdown parsed as prices
    "stock_transaction_ticks",  # undated rows marked 299 symbols complete
    "shareholder_records",      # every roster keyed on date=""
    "codal_announcements",      # Persian-digit dates + only page 1 of ~51
    "crypto_daily",             # 547 coins collapsed onto one symbol
    "commodity_daily",          # dict-of-lists payload never parsed
    "gold_daily",               # USDT rows land as USDT_IRT
]

PERSIAN_DIGITS = "[۰-۹٠-٩]"


def purge(apps, schema_editor):
    DailyStockHistory = apps.get_model("marketdata", "DailyStockHistory")
    StockTransactionTick = apps.get_model("marketdata", "StockTransactionTick")
    ShareholderRecord = apps.get_model("marketdata", "ShareholderRecord")
    CodalAnnouncement = apps.get_model("marketdata", "CodalAnnouncement")
    CryptoHistory = apps.get_model("marketdata", "CryptoHistory")
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")

    counts = {}
    # History.php?type=1 carries no price fields, so every one of these rows has
    # pf/pl/pc/pmin/pmax/tvol = 0. The Real/Legal columns now update the
    # is_adjusted=False row instead, so this whole partition is dead weight.
    counts["daily_history_adjusted"] = DailyStockHistory.objects.filter(
        is_adjusted=True
    ).delete()[0]

    # Undated ticks live in a different keyspace than (symbol, date, row), so a
    # correct re-fetch would never overwrite them -- they would sit alongside the
    # real rows forever and double-count any volume aggregate.
    counts["undated_ticks"] = StockTransactionTick.objects.filter(date="").delete()[0]

    counts["undated_shareholders"] = ShareholderRecord.objects.filter(
        date=""
    ).delete()[0]

    # Unjoinable against every other table; the corrected ingest writes ASCII, so
    # leaving these would duplicate each announcement under two date spellings.
    counts["persian_digit_codal"] = CodalAnnouncement.objects.filter(
        date_publish__regex=PERSIAN_DIGITS
    ).delete()[0]

    # Placeholder symbol: 547 coins per response all collided on ("CRYPTO", date).
    counts["placeholder_crypto"] = CryptoHistory.objects.filter(
        symbol="CRYPTO"
    ).delete()[0]

    counts["states_rearmed"] = ArchiveFetchState.objects.filter(
        endpoint__in=AFFECTED_ENDPOINTS
    ).update(
        verified_complete=False,
        next_attempt_at=None,
        last_attempt_at=None,
        last_success_at=None,
        expected_rows=0,
        stored_rows=0,
        missing_rows=0,
        consecutive_failures=0,
        last_error="",
    )
    print(f"  purge_unverifiable_rows: {counts}")


def rearm_only(apps, schema_editor):
    """Reverse: the rows held no information, so only the states are restored."""
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")
    ArchiveFetchState.objects.filter(endpoint__in=AFFECTED_ENDPOINTS).update(
        verified_complete=False, next_attempt_at=None, last_attempt_at=None
    )


class Migration(migrations.Migration):

    dependencies = [("marketdata", "0008_drop_live_archive_states")]

    operations = [migrations.RunPython(purge, rearm_only)]

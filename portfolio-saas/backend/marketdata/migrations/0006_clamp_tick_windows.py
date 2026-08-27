"""Pull runaway tick windows back under MAX_TICK_WINDOW_DAYS.

`grow_tick_windows` widened a state by 90 days every time it reported
`verified_complete`, and for symbols with no `InstrumentListingHistory` row the
rule was "grow indefinitely". `target_window_days` is a
PositiveSmallIntegerField, so indefinitely means 32,767: on 2026-08-27 thirty-one
states reached 32,760 and the next `+90` raised a database error inside the same
`try` that dispatches archive work, stopping all backfill for thirteen hours.

The code fix clamps growth and skips states already at the clamp. That stops the
crash but leaves these rows carrying a ~90-year fetch window, which is what makes
them claim, fetch nothing, and be re-claimed. This corrects them once.

Data-only and idempotent -- re-running selects nothing.
"""
from django.db import migrations

# Imported rather than duplicated so there is one definition of the ceiling.
from marketdata.archive import MAX_TICK_WINDOW_DAYS


def clamp(apps, schema_editor):
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")
    ArchiveFetchState.objects.filter(
        target_window_days__gt=MAX_TICK_WINDOW_DAYS
    ).update(target_window_days=MAX_TICK_WINDOW_DAYS)


def noop(apps, schema_editor):
    """No reverse: the original values were corrupt, not meaningful."""


class Migration(migrations.Migration):

    dependencies = [("marketdata", "0005_dedupe_ts_drift")]

    operations = [migrations.RunPython(clamp, noop)]

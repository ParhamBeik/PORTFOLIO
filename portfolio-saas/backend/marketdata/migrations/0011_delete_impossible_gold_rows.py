"""Delete gold/currency rows whose high is below their low.

54 rows across the 122,980-row table report a session high beneath the session
low, which cannot happen. There is no way to know which leg the provider got
wrong, so the row is removed rather than guessed at; the corrected ingest now
screens the same condition (`high_below_low`) before storing, and any future
occurrence lands in RejectedRecord with the payload attached instead of in the
price series.

Also clears the matching gold states so the affected symbols re-fetch and the
now-screened history lands clean.
"""
from django.db import migrations
from django.db.models import F


def delete_impossible(apps, schema_editor):
    GoldCurrencyHistory = apps.get_model("marketdata", "GoldCurrencyHistory")
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")

    doomed = GoldCurrencyHistory.objects.filter(high_price__lt=F("low_price"))
    symbols = sorted(set(doomed.values_list("symbol", flat=True)))
    deleted = doomed.delete()[0]

    rearmed = ArchiveFetchState.objects.filter(
        endpoint="gold_daily", symbol__in=symbols
    ).update(verified_complete=False, next_attempt_at=None, last_attempt_at=None)
    print(
        f"  delete_impossible_gold_rows: deleted={deleted} "
        f"symbols={symbols} states_rearmed={rearmed}"
    )


class Migration(migrations.Migration):

    dependencies = [("marketdata", "0010_validation_and_tick_key")]

    operations = [
        migrations.RunPython(delete_impossible, migrations.RunPython.noop),
    ]

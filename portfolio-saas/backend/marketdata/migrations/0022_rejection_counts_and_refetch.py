"""Repair what the old rejection bookkeeping got wrong, once, on deploy.

* `occurrences` counted downloads, not bad payloads: every full-history refresh
  re-sent every old bad row, so 23,260 rows read as 284,412. Nothing recorded
  which sightings carried a new payload, so the honest floor is one per row.
* Gold/FX rows rejected by the one-sided outlier screen, and real/legal days
  rejected over sub-1% rounding, are recovered by the provider's own payload
  once re-ingested under the fixed rules (`ingest.release`). Waking those
  states now makes that happen on the next archive pass instead of whenever
  their refresh comes round.
"""
from django.db import migrations


def repair(apps, schema_editor):
    RejectedRecord = apps.get_model("marketdata", "RejectedRecord")
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")
    RejectedRecord.objects.filter(occurrences__gt=1).update(occurrences=1)
    real_legal = set(
        RejectedRecord.objects.filter(
            endpoint="real_legal_history", reason="buy_sell_volume_mismatch",
        ).values_list("symbol", flat=True)
    )
    ArchiveFetchState.objects.filter(endpoint="gold_daily").update(next_attempt_at=None)
    ArchiveFetchState.objects.filter(
        endpoint="stock_history_unadjusted", symbol__in=real_legal,
    ).update(next_attempt_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0021_codal_direct_discovery"),
    ]

    operations = [
        migrations.RunPython(repair, migrations.RunPython.noop),
    ]

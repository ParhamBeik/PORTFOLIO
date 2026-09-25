from django.db import migrations, models


def backfill_baseline(apps, schema_editor):
    quota = apps.get_model("marketdata", "ApiRequestQuota")
    for row in quota.objects.filter(plan__in=("aio", "market_cgcc"), provider_used__isnull=False):
        # Product rows were introduced mid-day. The admission counter was
        # seeded from the first panel read, then incremented by local attempts.
        # Preserve that pre-observation spend instead of calling it variance.
        row.provider_baseline_used = min(
            row.provider_used, max(0, row.used - row.local_attempts)
        )
        row.save(update_fields=["provider_baseline_used"])


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0011_provider_meter_observation")]

    operations = [
        migrations.AddField(
            model_name="apirequestquota",
            name="provider_baseline_used",
            field=models.PositiveIntegerField(null=True, blank=True),
        ),
        migrations.RunPython(backfill_baseline, migrations.RunPython.noop),
    ]

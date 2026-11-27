"""Per-(symbol, day) tick summary (VPS load fix P4).

Two empty tables; nothing is backfilled here. `manage.py backfill_tick_summary`
fills them off-hours, symbol by symbol, and readers use a symbol's summary only
once `TickSummaryCoverage` says it is complete.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0023_codal_direct_identity"),
    ]

    operations = [
        migrations.CreateModel(
            name="TickDaySummary",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("symbol", models.CharField(max_length=64)),
                ("date", models.CharField(max_length=10)),
                ("ticks", models.PositiveIntegerField()),
                ("live_ticks", models.PositiveIntegerField()),
                ("live_volume", models.BigIntegerField()),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("symbol", "date"), name="uniq_tick_day_summary"),
                ],
            },
        ),
        migrations.CreateModel(
            name="TickSummaryCoverage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("symbol", models.CharField(max_length=64, unique=True)),
                ("backfilled_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]

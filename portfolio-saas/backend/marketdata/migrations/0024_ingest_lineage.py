from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketdata", "0023_operationalmetricsnapshot_ops_payload"),
    ]

    operations = [
        migrations.AddField(
            model_name="dailystockhistory",
            name="ingested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dailystockhistory",
            name="last_correlation_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="marketcandle",
            name="ingested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="marketcandle",
            name="last_correlation_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="goldcurrencyhistory",
            name="ingested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="goldcurrencyhistory",
            name="last_correlation_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
    ]

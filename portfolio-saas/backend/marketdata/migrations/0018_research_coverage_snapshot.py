from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0017_gold_currency_origin")]

    operations = [
        migrations.CreateModel(
            name="ResearchCoverageSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("started_at", models.DateTimeField()),
                ("finished_at", models.DateTimeField(db_index=True)),
                ("window_days", models.PositiveSmallIntegerField()),
                ("start_jalali", models.CharField(max_length=10)),
                ("end_jalali", models.CharField(max_length=10)),
                ("universe_size", models.PositiveIntegerField()),
                ("eligibility_version", models.CharField(max_length=32)),
                ("parser_versions", models.JSONField(default=dict)),
                ("summary", models.JSONField(default=dict)),
                ("symbols", models.JSONField(default=list)),
            ],
            options={"ordering": ["-finished_at"]},
        ),
    ]

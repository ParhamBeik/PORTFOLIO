from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0018_research_coverage_snapshot")]

    operations = [
        migrations.CreateModel(
            name="CodalHistoryWindow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("symbol", models.CharField(max_length=64)),
                ("date_start", models.CharField(max_length=10)),
                ("date_end", models.CharField(max_length=10)),
                ("expected_rows", models.PositiveIntegerField(default=0)),
                ("stored_rows", models.PositiveIntegerField(default=0)),
                ("verified_complete", models.BooleanField(default=False)),
                ("split", models.BooleanField(default=False)),
                ("consecutive_failures", models.PositiveIntegerField(default=0)),
                ("last_error", models.CharField(blank=True, default="", max_length=500)),
                ("last_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("last_success_at", models.DateTimeField(blank=True, null=True)),
                ("next_attempt_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["verified_complete", "split", "next_attempt_at"],
                        name="codal_history_due_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=["symbol", "date_start", "date_end"],
                        name="uniq_codal_history_window",
                    )
                ],
            },
        ),
    ]

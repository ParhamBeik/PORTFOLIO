from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0015_daily_history_index")]

    operations = [
        migrations.CreateModel(
            name="InstrumentListingHistory",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("symbol", models.CharField(max_length=64, unique=True)),
                ("first_seen", models.CharField(max_length=10)),
                ("last_seen", models.CharField(max_length=10)),
                (
                    "eligible_from",
                    models.CharField(blank=True, max_length=10, null=True),
                ),
                (
                    "eligible_to",
                    models.CharField(blank=True, max_length=10, null=True),
                ),
            ],
            options={"ordering": ["symbol"]},
        )
    ]

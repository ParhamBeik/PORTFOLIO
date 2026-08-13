from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketdata", "0021_codalartifact_archivefetchstate_archive_cursor_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationalMetricSnapshot",
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
                ("captured_at", models.DateTimeField(unique=True)),
                ("database_counts", models.JSONField(default=dict)),
            ],
            options={"ordering": ["-captured_at"]},
        ),
    ]

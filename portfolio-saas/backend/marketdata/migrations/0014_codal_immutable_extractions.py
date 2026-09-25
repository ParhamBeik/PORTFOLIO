import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0013_codal_verification_status")]

    operations = [
        migrations.CreateModel(
            name="CodalExtraction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("checksum_sha256", models.CharField(max_length=64)),
                ("parser_version", models.CharField(max_length=32)),
                ("parsed_at", models.DateTimeField(auto_now_add=True)),
                ("table_count", models.PositiveIntegerField(default=0)),
                ("section_count", models.PositiveIntegerField(default=0)),
                ("fact_count", models.PositiveIntegerField(default=0)),
                ("artifact", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="extractions", to="marketdata.codalartifact")),
                ("report", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="extractions", to="marketdata.codalreport")),
            ],
        ),
        migrations.CreateModel(
            name="CodalCandidateFact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fact_code", models.CharField(max_length=160)),
                ("raw_value", models.TextField(blank=True, default="")),
                ("numeric_value", models.DecimalField(blank=True, decimal_places=12, max_digits=38, null=True)),
                ("unit", models.CharField(blank=True, default="", max_length=64)),
                ("currency", models.CharField(blank=True, default="", max_length=16)),
                ("period_start", models.CharField(blank=True, default="", max_length=10)),
                ("period_end", models.CharField(blank=True, default="", max_length=10)),
                ("dimensions", models.JSONField(default=dict)),
                ("source_coordinates", models.JSONField(default=dict)),
                ("verification_status", models.CharField(
                    choices=[
                        ("legacy_unverified", "Legacy, unverified"),
                        ("extracted", "Extracted, unverified"),
                        ("reconciled", "Source reconciled"),
                        ("quarantined", "Quarantined"),
                    ],
                    default="extracted", max_length=24,
                )),
                ("extraction", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="candidates", to="marketdata.codalextraction")),
            ],
        ),
        migrations.AddConstraint(
            model_name="codalextraction",
            constraint=models.UniqueConstraint(
                fields=("report", "artifact", "checksum_sha256", "parser_version"),
                name="uniq_codal_extraction_bytes_parser",
            ),
        ),
    ]

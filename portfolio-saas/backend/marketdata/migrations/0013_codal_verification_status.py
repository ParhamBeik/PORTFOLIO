from django.db import migrations, models


VERIFICATION_CHOICES = [
    ("legacy_unverified", "Legacy, unverified"),
    ("extracted", "Extracted, unverified"),
    ("reconciled", "Source reconciled"),
    ("quarantined", "Quarantined"),
]


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0012_provider_usage_baseline")]

    operations = [
        migrations.AddField(
            model_name="codalreport",
            name="verification_status",
            field=models.CharField(
                max_length=24, choices=VERIFICATION_CHOICES,
                default="legacy_unverified",
            ),
        ),
        migrations.AddField(
            model_name="codalfact",
            name="verification_status",
            field=models.CharField(
                max_length=24, choices=VERIFICATION_CHOICES,
                default="legacy_unverified",
            ),
        ),
    ]

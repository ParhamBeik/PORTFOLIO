"""Rename CryptoHistory.close_price_toman to close_price_rial.

Field-name-only migration; the historical x10 data conversion is a separate
migration (0019_rial_storage_values) driven by the audit_price_units manifest.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0017_asset_metric_snapshot"),
    ]

    operations = [
        migrations.RenameField(
            model_name="cryptohistory",
            old_name="close_price_toman",
            new_name="close_price_rial",
        ),
    ]

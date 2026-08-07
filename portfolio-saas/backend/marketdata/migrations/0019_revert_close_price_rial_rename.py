"""Rename CryptoHistory.close_price_rial back to close_price_toman.

Forward-only fix for 0018: this session's currency-normalization attempt
renamed the field to `close_price_rial` and planned a follow-up value
conversion (the now-deleted 0019_rial_storage_values) that never ran. The
raw-storage policy stores the provider's `price_toman` field verbatim, so the
column name should say what it holds. No value change -- pure rename.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0018_rename_close_price_toman"),
    ]

    operations = [
        migrations.RenameField(
            model_name="cryptohistory",
            old_name="close_price_rial",
            new_name="close_price_toman",
        ),
    ]

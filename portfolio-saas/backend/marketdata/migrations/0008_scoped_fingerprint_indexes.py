from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("marketdata", "0007_cleanup_crypto_gold_daily_states"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="dailystockhistory",
            index=models.Index(fields=["symbol", "-id"], name="stockhist_symbol_id_idx"),
        ),
        AddIndexConcurrently(
            model_name="marketcandle",
            index=models.Index(fields=["symbol", "-id"], name="candle_symbol_id_idx"),
        ),
    ]

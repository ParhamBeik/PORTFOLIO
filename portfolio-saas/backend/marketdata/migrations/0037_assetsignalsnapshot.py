"""AssetSignalSnapshot: nightly technical reading per symbol.

Hand-written rather than generated, because the environment this was authored in
had no Python available to run `makemigrations`. It mirrors AssetMetricSnapshot's
shape exactly, so `makemigrations --check` should report no drift; if it does,
trust the generator and replace this file.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0036_daily_tables_back_to_plain"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetSignalSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("symbol", models.CharField(db_index=True, max_length=64)),
                ("asset_class", models.CharField(blank=True, default="", max_length=16)),
                ("as_of", models.CharField(db_index=True, max_length=10)),
                ("window_days", models.PositiveSmallIntegerField(default=365)),
                ("rsi", models.FloatField(blank=True, null=True)),
                ("macd_histogram", models.FloatField(blank=True, null=True)),
                ("above_trend", models.BooleanField(default=False)),
                ("trend_window", models.PositiveSmallIntegerField(default=0)),
                ("overbought", models.BooleanField(default=False)),
                ("oversold", models.BooleanField(default=False)),
                ("stance", models.CharField(
                    choices=[("bullish", "Bullish"), ("bearish", "Bearish"),
                             ("neutral", "Neutral")],
                    db_index=True, default="neutral", max_length=8)),
                ("observations", models.PositiveIntegerField(default=0)),
                ("passes_integrity", models.BooleanField(db_index=True, default=False)),
            ],
            options={"ordering": ["-as_of", "symbol"]},
        ),
        migrations.AddConstraint(
            model_name="assetsignalsnapshot",
            constraint=models.UniqueConstraint(
                fields=("symbol", "as_of", "window_days"),
                name="uniq_asset_signal_symbol_asof_window",
            ),
        ),
    ]

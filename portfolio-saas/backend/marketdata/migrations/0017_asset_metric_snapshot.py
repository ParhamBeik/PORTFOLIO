from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0016_instrument_listing_history")]

    operations = [
        migrations.CreateModel(
            name="AssetMetricSnapshot",
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
                ("symbol", models.CharField(db_index=True, max_length=64)),
                (
                    "asset_class",
                    models.CharField(blank=True, default="", max_length=16),
                ),
                ("as_of", models.CharField(db_index=True, max_length=10)),
                (
                    "window_days",
                    models.PositiveSmallIntegerField(default=365),
                ),
                ("total_return", models.FloatField(default=0.0)),
                ("annualized_volatility", models.FloatField(default=0.0)),
                ("sharpe", models.FloatField(default=0.0)),
                ("sortino", models.FloatField(default=0.0)),
                ("max_drawdown", models.FloatField(default=0.0)),
                ("beta", models.FloatField(blank=True, null=True)),
                ("correlation", models.FloatField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-as_of", "-sharpe"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("symbol", "as_of", "window_days"),
                        name="uniq_asset_metric_symbol_asof_window",
                    )
                ],
            },
        )
    ]

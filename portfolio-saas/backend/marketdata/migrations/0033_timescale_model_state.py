"""Align Django's state with the composite indexes created by migration 0032."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("marketdata", "0032_hypertables")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="dailystockhistory",
                    name="ts",
                    field=models.DateTimeField(blank=True),
                ),
                migrations.AlterField(
                    model_name="marketcandle",
                    name="ts",
                    field=models.DateTimeField(blank=True),
                ),
                migrations.AlterField(
                    model_name="stocktransactiontick",
                    name="ts",
                    field=models.DateTimeField(blank=True),
                ),
                migrations.RemoveConstraint(
                    model_name="dailystockhistory",
                    name="uniq_stock_history_symbol_date_adj",
                ),
                migrations.AddConstraint(
                    model_name="dailystockhistory",
                    constraint=models.UniqueConstraint(
                        fields=("symbol", "date", "is_adjusted", "ts"),
                        name="uniq_stock_history_symbol_date_adj",
                    ),
                ),
                migrations.RemoveConstraint(
                    model_name="marketcandle",
                    name="uniq_market_candle_symbol_tf_dt",
                ),
                migrations.AddConstraint(
                    model_name="marketcandle",
                    constraint=models.UniqueConstraint(
                        fields=("symbol", "timeframe", "date_time", "ts"),
                        name="uniq_market_candle_symbol_tf_dt",
                    ),
                ),
                migrations.RemoveConstraint(
                    model_name="stocktransactiontick",
                    name="uniq_stock_tick_symbol_date_row_time",
                ),
                migrations.AddConstraint(
                    model_name="stocktransactiontick",
                    constraint=models.UniqueConstraint(
                        fields=("symbol", "date", "row", "time", "ts"),
                        name="uniq_stock_tick_symbol_date_row_time",
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]

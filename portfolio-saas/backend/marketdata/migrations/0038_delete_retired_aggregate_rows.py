"""Delete retired live-tick-derived AGGREGATE rows.

Do NOT run this until `portfolio.tasks.aggregate_daily_price_averages` has
been populating `DailyPriceAverage` for at least one full day/night cycle --
this is the last step of the live/historical data separation, removed only
once the new daily-average table is confirmed populating correctly. See the
project plan for the full step ordering.
"""
from django.db import migrations


def delete_aggregate_rows(apps, schema_editor):
    MarketCandle = apps.get_model("marketdata", "MarketCandle")
    GoldCurrencyHistory = apps.get_model("marketdata", "GoldCurrencyHistory")
    MarketCandle.objects.filter(timeframe="1d_agg").delete()
    GoldCurrencyHistory.objects.filter(source="aggregate").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0037_assetsignalsnapshot"),
    ]

    operations = [
        migrations.RunPython(delete_aggregate_rows, migrations.RunPython.noop),
    ]

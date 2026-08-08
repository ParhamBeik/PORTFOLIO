from django.db import migrations, models


def remove_retired_index_state(apps, schema_editor):
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")
    ArchiveFetchState.objects.filter(endpoint="market_index_daily").delete()


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0019_revert_close_price_rial_rename")]

    operations = [
        migrations.AddField(
            model_name="archivefetchstate",
            name="known_gap_rows",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="goldcurrencyhistory",
            name="source",
            field=models.CharField(
                choices=[("provider", "Provider"), ("aggregate", "Live-price aggregate")],
                default="provider",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="cryptohistory",
            name="close_price_usd",
            field=models.DecimalField(decimal_places=12, default=0, max_digits=30),
        ),
        migrations.AlterField(
            model_name="dailystockhistory",
            name="pf",
            field=models.DecimalField(
                blank=True, decimal_places=4, default=0, max_digits=20, null=True
            ),
        ),
        migrations.AlterField(
            model_name="dailystockhistory",
            name="pmax",
            field=models.DecimalField(
                blank=True, decimal_places=4, default=0, max_digits=20, null=True
            ),
        ),
        migrations.AlterField(
            model_name="dailystockhistory",
            name="pmin",
            field=models.DecimalField(
                blank=True, decimal_places=4, default=0, max_digits=20, null=True
            ),
        ),
        migrations.AlterField(
            model_name="marketcandle",
            name="high_price",
            field=models.DecimalField(
                blank=True, decimal_places=4, default=0, max_digits=20, null=True
            ),
        ),
        migrations.AlterField(
            model_name="marketcandle",
            name="low_price",
            field=models.DecimalField(
                blank=True, decimal_places=4, default=0, max_digits=20, null=True
            ),
        ),
        migrations.AlterField(
            model_name="marketcandle",
            name="open_price",
            field=models.DecimalField(
                blank=True, decimal_places=4, default=0, max_digits=20, null=True
            ),
        ),
        migrations.RunPython(remove_retired_index_state, migrations.RunPython.noop),
    ]

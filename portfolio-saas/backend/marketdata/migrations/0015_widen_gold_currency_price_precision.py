from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0014_codal_immutable_extractions")]

    operations = [
        migrations.AlterField(
            model_name="goldcurrencyhistory", name="open_price",
            field=models.DecimalField(max_digits=30, decimal_places=12, null=True, blank=True),
        ),
        migrations.AlterField(
            model_name="goldcurrencyhistory", name="high_price",
            field=models.DecimalField(max_digits=30, decimal_places=12, null=True, blank=True),
        ),
        migrations.AlterField(
            model_name="goldcurrencyhistory", name="low_price",
            field=models.DecimalField(max_digits=30, decimal_places=12, null=True, blank=True),
        ),
        migrations.AlterField(
            model_name="goldcurrencyhistory", name="close_price",
            field=models.DecimalField(max_digits=30, decimal_places=12, default=0),
        ),
    ]

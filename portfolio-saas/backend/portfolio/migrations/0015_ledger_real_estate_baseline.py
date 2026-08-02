from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0014_backtest_contract")]

    operations = [
        migrations.AddField(
            model_name="ledgerentry",
            name="area_sqm",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True
            ),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="mortgage_deduction_tomans",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=20, null=True
            ),
        ),
    ]

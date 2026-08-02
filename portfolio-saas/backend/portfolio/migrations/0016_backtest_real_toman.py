from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0015_ledger_real_estate_baseline")]

    operations = [
        migrations.AlterField(
            model_name="backtestrun",
            name="basis",
            field=models.CharField(
                choices=[
                    ("nominal_toman", "nominal_toman"),
                    ("usd_denominated", "usd_denominated"),
                    ("real_toman", "real_toman"),
                    ("nominal", "nominal (deprecated)"),
                    ("usd_real", "usd_real (deprecated)"),
                ],
                default="nominal_toman",
                max_length=20,
            ),
        )
    ]

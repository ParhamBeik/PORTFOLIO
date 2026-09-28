from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0016_codal_source_category")]

    operations = [
        migrations.AddField(
            model_name="goldcurrencyhistory",
            name="origin",
            field=models.CharField(
                max_length=16, default="unknown",
                choices=[
                    ("unknown", "Historic source unknown"),
                    ("brsapi", "BrsApi"),
                    ("tgju", "TGJU"),
                    ("wallex", "Wallex"),
                ],
            ),
        ),
    ]

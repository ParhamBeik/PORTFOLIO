from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0014_corporate_action")]

    operations = [
        migrations.AddIndex(
            model_name="dailystockhistory",
            index=models.Index(
                fields=["symbol", "date"],
                name="marketdata__symbol_7ee44e_idx",
            ),
        ),
        migrations.RemoveIndex(
            model_name="goldcurrencyhistory",
            name="marketdata__symbol_51bcbf_idx",
        ),
    ]

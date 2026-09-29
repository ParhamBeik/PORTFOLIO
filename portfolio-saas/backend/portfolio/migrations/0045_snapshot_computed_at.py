from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0044_split_quote_prices")]

    operations = [
        migrations.AddField(
            model_name="snapshot",
            name="computed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

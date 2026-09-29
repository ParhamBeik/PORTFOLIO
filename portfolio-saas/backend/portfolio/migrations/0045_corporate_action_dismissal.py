import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portfolio", "0044_split_quote_prices"),
    ]

    operations = [
        migrations.CreateModel(
            name="CorporateActionDismissal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("symbol", models.CharField(max_length=64)),
                ("date", models.CharField(max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="corporate_action_dismissals",
                    to="portfolio.account",
                )),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("account", "symbol", "date"),
                        name="uniq_corporate_action_dismissal",
                    )
                ],
            },
        ),
    ]

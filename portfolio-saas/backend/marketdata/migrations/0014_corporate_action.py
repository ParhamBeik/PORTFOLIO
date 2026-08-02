from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0013_symbolintegrity_and_more")]

    operations = [
        migrations.CreateModel(
            name="CorporateAction",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("symbol", models.CharField(db_index=True, max_length=64)),
                ("date", models.CharField(db_index=True, max_length=10)),
                (
                    "factor",
                    models.DecimalField(decimal_places=10, max_digits=20),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("split", "Split"),
                            ("capital_increase", "Capital increase"),
                            ("dividend", "Dividend"),
                            ("unknown", "Unknown"),
                        ],
                        default="unknown",
                        max_length=24,
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            (
                                "derived_from_factor_ratio",
                                "Derived from factor ratio",
                            ),
                            ("codal", "Codal"),
                        ],
                        default="derived_from_factor_ratio",
                        max_length=32,
                    ),
                ),
            ],
            options={
                "ordering": ["symbol", "-date"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("symbol", "date"),
                        name="uniq_corporate_action_symbol_date",
                    )
                ],
            },
        )
    ]

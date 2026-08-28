"""Register the rights-issue (افزایش سرمایه) ledger kind.

Choices-only: no column changes, no data touched. It exists so
`makemigrations --check` in CI stays clean, and so the new kind is part of the
recorded model state rather than only the enum.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("portfolio", "0031_true_tse_share_counts")]

    operations = [
        migrations.AlterField(
            model_name="ledgerentry",
            name="kind",
            field=models.CharField(
                max_length=24,
                choices=[
                    ("opening_position", "Opening position"),
                    ("opening_cash", "Opening cash"),
                    ("deposit", "Deposit"),
                    ("withdrawal", "Withdrawal"),
                    ("buy", "Buy"),
                    ("sell", "Sell"),
                    ("dividend", "Dividend"),
                    ("fee", "Fee"),
                    ("valuation_mark", "Valuation mark"),
                    ("rights_issue", "Rights issue (افزایش سرمایه)"),
                ],
            ),
        ),
    ]

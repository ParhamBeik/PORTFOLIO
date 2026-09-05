"""Loan terms on `Liability`, and a declared cost basis on `LedgerEntry`.

The `kind` backfill is the only data step and it is a pure classification of
what the rows already say: a liability that names an asset was secured against
it, one that does not was not. Nothing is invented and nothing is destroyed, so
the reverse is a no-op rather than a guess at which rows used to be `other`.
"""
import django.utils.timezone
from django.db import migrations, models


def classify_existing(apps, schema_editor):
    Liability = apps.get_model("portfolio", "Liability")
    Liability.objects.filter(asset__isnull=False).update(kind="secured_debt")
    Liability.objects.filter(asset__isnull=True).update(kind="other")


class Migration(migrations.Migration):

    dependencies = [
        ("portfolio", "0033_optimizationsnapshot_basis_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="ledgerentry",
            name="cost_basis_tomans",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=20, null=True
            ),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="updated_at",
            field=models.DateTimeField(
                auto_now=True, default=django.utils.timezone.now
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="liability",
            name="kind",
            field=models.CharField(
                choices=[
                    ("bank_loan", "Bank loan"),
                    ("secured_debt", "Debt secured on an asset"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="liability",
            name="lender",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Bank or institution the money is owed to.",
                max_length=120,
            ),
        ),
        migrations.AddField(
            model_name="liability",
            name="principal_tomans",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                help_text="Amount originally borrowed.",
                max_digits=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="liability",
            name="annual_rate_pct",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text="Nominal annual rate, percent (18 means 18%).",
                max_digits=6,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="liability",
            name="term_months",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Total number of monthly installments.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="liability",
            name="monthly_installment_tomans",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                help_text="Installment actually paid, when it is known but the rate is not.",
                max_digits=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="liability",
            name="started_on",
            field=models.DateField(
                blank=True,
                help_text="Date the first installment was due.",
                null=True,
            ),
        ),
        migrations.RunPython(classify_existing, migrations.RunPython.noop),
    ]

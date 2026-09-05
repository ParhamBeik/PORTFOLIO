"""Mark the liabilities the ledger replay owns, so it stops eating the rest.

`rebuild_projections` recreates a house's mortgage on every replay and deletes
the previous copy first. That reap was keyed on `asset__isnull=False`, which was
a safe proxy only while the mark path was the sole writer of asset-linked rows;
0034 gave the API a `secured_debt` kind that requires an asset, so the proxy
started matching debts a person had entered by hand.

The backfill marks exactly the mark path's own signature — a row named
`Mortgage (...)`, secured on an asset, carrying no repayment schedule, since
that path has never written one. Anything else is treated as hand-entered,
which is the direction that cannot lose data: a derived row wrongly left
unflagged is re-derived on the next replay, while a hand-entered row wrongly
flagged would be deleted by it.
"""
from django.db import migrations, models


def flag_derived(apps, schema_editor):
    Liability = apps.get_model("portfolio", "Liability")
    Liability.objects.filter(
        asset__isnull=False,
        label__startswith="Mortgage (",
        principal_tomans__isnull=True,
        annual_rate_pct__isnull=True,
        term_months__isnull=True,
        monthly_installment_tomans__isnull=True,
        started_on__isnull=True,
    ).update(derived=True)


class Migration(migrations.Migration):

    dependencies = [
        ("portfolio", "0034_liability_loan_terms_and_cost_basis"),
    ]

    operations = [
        migrations.AddField(
            model_name="liability",
            name="derived",
            field=models.BooleanField(
                default=False,
                help_text="Maintained by the ledger replay rather than entered by hand.",
            ),
        ),
        # Reverse is a no-op: dropping the column is the whole undo, and there is
        # nothing to restore that the classification did not read off the row.
        migrations.RunPython(flag_derived, migrations.RunPython.noop),
    ]

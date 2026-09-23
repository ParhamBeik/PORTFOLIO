from django.db import migrations, models


FIELDS = (
    ("portfolio_account", "cash_balance_tomans"),
    ("portfolio_holding", "mortgage_deduction_tomans"),
    ("portfolio_ledgerentry", "amount_tomans"),
    ("portfolio_ledgerentry", "mortgage_deduction_tomans"),
    ("portfolio_snapshot", "total_value_tomans"),
    ("portfolio_liability", "amount_tomans"),
    ("portfolio_liability", "principal_tomans"),
    ("portfolio_liability", "monthly_installment_tomans"),
)


def audit_before_rounding(apps, schema_editor):
    # PostgreSQL round(numeric, 0) rounds ties away from zero, matching
    # Decimal.ROUND_HALF_UP. The audit is inserted before ALTER COLUMN loses
    # the original fraction; it remains queryable even if a source row changes.
    with schema_editor.connection.cursor() as cursor:
        for table, field in FIELDS:
            cursor.execute(
                f"""
                INSERT INTO portfolio_monetaryroundingaudit
                    (source_table, source_id, field_name,
                     before_value, after_value, delta)
                SELECT %s, id, %s, {field}, round({field}, 0),
                       round({field}, 0) - {field}
                FROM {table}
                WHERE {field} IS NOT NULL AND {field} <> round({field}, 0)
                """,
                [table, field],
            )


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0040_daily_snapshot")]

    operations = [
        migrations.CreateModel(
            name="MonetaryRoundingAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_table", models.CharField(max_length=80)),
                ("source_id", models.BigIntegerField()),
                ("field_name", models.CharField(max_length=80)),
                ("before_value", models.DecimalField(decimal_places=6, max_digits=30)),
                ("after_value", models.DecimalField(decimal_places=0, max_digits=30)),
                ("delta", models.DecimalField(decimal_places=6, max_digits=30)),
            ],
            options={"constraints": [models.UniqueConstraint(
                fields=("source_table", "source_id", "field_name"),
                name="uniq_monetary_rounding_audit_source",
            )]},
        ),
        migrations.RunPython(audit_before_rounding, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="account", name="cash_balance_tomans",
            field=models.DecimalField(max_digits=24, decimal_places=0, default=0),
        ),
        migrations.AlterField(
            model_name="holding", name="mortgage_deduction_tomans",
            field=models.DecimalField(max_digits=20, decimal_places=0, default=0),
        ),
        migrations.AlterField(
            model_name="ledgerentry", name="amount_tomans",
            field=models.DecimalField(max_digits=24, decimal_places=0, null=True, blank=True),
        ),
        migrations.AlterField(
            model_name="ledgerentry", name="mortgage_deduction_tomans",
            field=models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True),
        ),
        migrations.AlterField(
            model_name="snapshot", name="total_value_tomans",
            field=models.DecimalField(max_digits=24, decimal_places=0, default=0),
        ),
        migrations.AlterField(
            model_name="liability", name="amount_tomans",
            field=models.DecimalField(max_digits=20, decimal_places=0),
        ),
        migrations.AlterField(
            model_name="liability", name="principal_tomans",
            field=models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True, help_text="Amount originally borrowed."),
        ),
        migrations.AlterField(
            model_name="liability", name="monthly_installment_tomans",
            field=models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True, help_text="Installment actually paid, when it is known but the rate is not."),
        ),
    ]

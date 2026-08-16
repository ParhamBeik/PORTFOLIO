"""Swap `ts` to the field that derives itself, with no DDL.

The column is already `timestamptz NOT NULL` from 0032; only the Python side
changes, so this is state-only. Letting Django emit its usual
`ALTER COLUMN ... TYPE` here would rewrite three hypertables and can fail
outright on chunks the compression policy has already compressed.
"""
import marketdata.models
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0034_derivative_contract_snapshot"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="dailystockhistory",
                    name="ts",
                    field=marketdata.models.JalaliDerivedDateTime(
                        blank=True, date_field="date"
                    ),
                ),
                migrations.AlterField(
                    model_name="marketcandle",
                    name="ts",
                    field=marketdata.models.JalaliDerivedDateTime(
                        blank=True, date_field="date_time"
                    ),
                ),
                migrations.AlterField(
                    model_name="stocktransactiontick",
                    name="ts",
                    field=marketdata.models.JalaliDerivedDateTime(
                        blank=True, date_field="date", time_field="time"
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]

from django.db import migrations, models


def verify_shadow_matches_legacy(apps, schema_editor):
    """Do not drop the old decimal until every row reconstructs exactly."""
    with schema_editor.connection.cursor() as cursor:
        for table in ("portfolio_holding", "portfolio_ledgerentry"):
            cursor.execute(f"""
                SELECT count(*) FROM {table} AS row
                LEFT JOIN portfolio_asset AS asset ON asset.id = row.asset_id
                WHERE row.quantity IS DISTINCT FROM CASE
                    WHEN row.quantity IS NULL THEN NULL
                    WHEN asset.is_house THEN row.price_per_sqm_tomans / 1000000
                    ELSE row.quantity_atomic::numeric / CASE
                        WHEN asset.asset_class = 'Crypto'
                          OR asset.key IN ('gold_18k_gram', 'usdt_irt')
                        THEN 1000000 ELSE 1 END
                END
            """)
            if cursor.fetchone()[0]:
                raise ValueError(f"{table} atomic quantity does not match legacy data")


def audit_exact_conversion(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for table in ("portfolio_holding", "portfolio_ledgerentry"):
            cursor.execute(f"""
                INSERT INTO portfolio_quantityconversionaudit
                    (source_table, source_id, asset_id, before_quantity,
                     after_quantity_atomic, after_price_per_sqm_tomans,
                     quantity_scale)
                SELECT %s, row.id, row.asset_id, row.quantity,
                       row.quantity_atomic, row.price_per_sqm_tomans,
                       CASE WHEN asset.asset_class = 'Crypto'
                         OR asset.key IN ('gold_18k_gram', 'usdt_irt')
                       THEN 1000000 ELSE 1 END
                FROM {table} AS row
                JOIN portfolio_asset AS asset ON asset.id = row.asset_id
                WHERE row.quantity IS NOT NULL
            """, [table])


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0042_atomic_quantity_shadow")]

    operations = [
        migrations.CreateModel(
            name="QuantityConversionAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_table", models.CharField(max_length=80)),
                ("source_id", models.BigIntegerField()),
                ("asset_id", models.BigIntegerField()),
                ("before_quantity", models.DecimalField(max_digits=20, decimal_places=6)),
                ("after_quantity_atomic", models.BigIntegerField(null=True, blank=True)),
                ("after_price_per_sqm_tomans", models.DecimalField(max_digits=24, decimal_places=0, null=True, blank=True)),
                ("quantity_scale", models.PositiveIntegerField()),
            ],
            options={"constraints": [models.UniqueConstraint(
                fields=("source_table", "source_id"),
                name="uniq_quantity_conversion_audit_source",
            )]},
        ),
        migrations.RunPython(verify_shadow_matches_legacy, migrations.RunPython.noop),
        migrations.RunPython(audit_exact_conversion, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="ledgerentry", name="ledger_asset_event_fields",
        ),
        migrations.RemoveField(model_name="holding", name="quantity"),
        migrations.RemoveField(model_name="ledgerentry", name="quantity"),
        migrations.AddConstraint(
            model_name="holding",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(quantity_atomic__isnull=False, price_per_sqm_tomans__isnull=True)
                    | models.Q(quantity_atomic__isnull=True, price_per_sqm_tomans__isnull=False)
                ),
                name="holding_one_quantity_storage",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(quantity_atomic__isnull=True)
                    | models.Q(price_per_sqm_tomans__isnull=True)
                ),
                name="ledger_one_quantity_storage",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(kind__in=["opening_position", "buy", "sell", "valuation_mark"])
                    | (
                        models.Q(asset__isnull=False)
                        & (
                            models.Q(quantity_atomic__gt=0)
                            | models.Q(price_per_sqm_tomans__gt=0)
                        )
                    )
                ),
                name="ledger_asset_event_fields",
            ),
        ),
    ]

from django.db import migrations, models


def validate_exact_conversion(apps, schema_editor):
    """Fail rather than silently round an indivisible holding or event."""
    with schema_editor.connection.cursor() as cursor:
        for table in ("portfolio_holding", "portfolio_ledgerentry"):
            cursor.execute(f"""
                SELECT count(*) FROM {table} AS row
                JOIN portfolio_asset AS asset ON asset.id = row.asset_id
                WHERE row.quantity IS NOT NULL AND NOT asset.is_house
                  AND row.quantity * CASE
                    WHEN asset.asset_class = 'Crypto'
                      OR asset.key IN ('gold_18k_gram', 'usdt_irt')
                    THEN 1000000 ELSE 1 END
                    <> round(row.quantity * CASE
                    WHEN asset.asset_class = 'Crypto'
                      OR asset.key IN ('gold_18k_gram', 'usdt_irt')
                    THEN 1000000 ELSE 1 END, 0)
            """)
            if cursor.fetchone()[0]:
                raise ValueError(f"{table} has quantities below their asset atomic unit")


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0041_whole_toman_audit")]

    operations = [
        migrations.AddField(
            model_name="holding", name="quantity_atomic",
            field=models.BigIntegerField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="holding", name="price_per_sqm_tomans",
            field=models.DecimalField(max_digits=24, decimal_places=0, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="ledgerentry", name="quantity_atomic",
            field=models.BigIntegerField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="ledgerentry", name="price_per_sqm_tomans",
            field=models.DecimalField(max_digits=24, decimal_places=0, null=True, blank=True),
        ),
        migrations.RunPython(validate_exact_conversion, migrations.RunPython.noop),
        migrations.RunSQL(
            sql="""
                UPDATE portfolio_holding AS row SET
                    quantity_atomic = CASE WHEN asset.is_house THEN NULL ELSE
                        (row.quantity * CASE
                            WHEN asset.asset_class = 'Crypto'
                              OR asset.key IN ('gold_18k_gram', 'usdt_irt')
                            THEN 1000000 ELSE 1 END)::bigint END,
                    price_per_sqm_tomans = CASE WHEN asset.is_house
                        THEN round(row.quantity * 1000000, 0) ELSE NULL END
                FROM portfolio_asset AS asset WHERE asset.id = row.asset_id;
                UPDATE portfolio_ledgerentry AS row SET
                    quantity_atomic = CASE WHEN asset.is_house THEN NULL ELSE
                        (row.quantity * CASE
                            WHEN asset.asset_class = 'Crypto'
                              OR asset.key IN ('gold_18k_gram', 'usdt_irt')
                            THEN 1000000 ELSE 1 END)::bigint END,
                    price_per_sqm_tomans = CASE WHEN asset.is_house
                        THEN round(row.quantity * 1000000, 0) ELSE NULL END
                FROM portfolio_asset AS asset
                WHERE asset.id = row.asset_id AND row.quantity IS NOT NULL;
            """,
            reverse_sql="""
                UPDATE portfolio_holding SET quantity_atomic = NULL, price_per_sqm_tomans = NULL;
                UPDATE portfolio_ledgerentry SET quantity_atomic = NULL, price_per_sqm_tomans = NULL;
            """,
        ),
    ]

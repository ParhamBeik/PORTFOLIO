# Batched backfill to set price unit metadata efficiently.
# Replaces per-row ORM saves with SQL UPDATE statements to avoid long migration runtime.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('portfolio', '0020_add_price_unit_fields'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            -- Mark TSE-linked prices as UNKNOWN/unverified
            UPDATE portfolio_price p
            SET price_unit = 'UNKNOWN', price_unit_verified = false
            FROM portfolio_asset a
            WHERE p.asset_id = a.id AND a.tse_symbol IS NOT NULL;

            -- Mark BRS-linked prices as IRT/verified
            UPDATE portfolio_price p
            SET price_unit = 'IRT', price_unit_verified = true
            FROM portfolio_asset a
            WHERE p.asset_id = a.id AND a.brs_symbol IS NOT NULL;
            """,
            reverse_sql="""
            -- Revert to conservative default for all rows
            UPDATE portfolio_price
            SET price_unit = 'UNKNOWN', price_unit_verified = false;
            """,
        ),
    ]

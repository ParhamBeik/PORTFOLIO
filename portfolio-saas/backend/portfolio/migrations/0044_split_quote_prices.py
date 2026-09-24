from django.db import migrations, models


FOREIGN_KEYS = ("bitcoin_usd", "gold_ounce_usd")
SOURCES = (
    ("portfolio_price", "price"),
    ("portfolio_dailypriceaverage", "avg_price"),
    ("portfolio_ledgerentry", "price_tomans"),
)


def audit_and_copy(apps, schema_editor):
    """Audit every fractional Iranian quote before zero-decimal conversion."""
    with schema_editor.connection.cursor() as cursor:
        for table, field in SOURCES:
            cursor.execute(f"""
                INSERT INTO portfolio_monetaryroundingaudit
                    (source_table, source_id, field_name,
                     before_value, after_value, delta)
                SELECT %s, row.id, %s, row.{field}, round(row.{field}, 0),
                       round(row.{field}, 0) - row.{field}
                FROM {table} AS row
                LEFT JOIN portfolio_asset AS asset ON asset.id = row.asset_id
                WHERE (asset.key IS NULL OR NOT (asset.key = ANY(%s)))
                  AND row.{field} IS NOT NULL
                  AND row.{field} <> round(row.{field}, 0)
            """, [table, field, list(FOREIGN_KEYS)])
            cursor.execute(f"""
                UPDATE {table} AS row SET
                    {"avg_" if field == "avg_price" else ""}price_foreign = row.{field}
                WHERE row.asset_id IN (
                    SELECT id FROM portfolio_asset WHERE key = ANY(%s)
                ) AND row.{field} IS NOT NULL
            """, [list(FOREIGN_KEYS)])
            cursor.execute(f"""
                UPDATE {table} AS row SET
                    {"avg_" if field == "avg_price" else ""}price_iranian = round(row.{field}, 0)
                WHERE (row.asset_id IS NULL OR row.asset_id NOT IN (
                    SELECT id FROM portfolio_asset WHERE key = ANY(%s)
                )) AND row.{field} IS NOT NULL
            """, [list(FOREIGN_KEYS)])


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0043_atomic_quantity_cutover")]

    operations = [
        migrations.AddField("price", "price_iranian", models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True)),
        migrations.AddField("price", "price_foreign", models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)),
        migrations.AddField("dailypriceaverage", "avg_price_iranian", models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True)),
        migrations.AddField("dailypriceaverage", "avg_price_foreign", models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)),
        migrations.AddField("ledgerentry", "price_iranian", models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True)),
        migrations.AddField("ledgerentry", "price_foreign", models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)),
        migrations.RunPython(audit_and_copy, migrations.RunPython.noop),
        migrations.RemoveField("price", "price"),
        migrations.RemoveField("dailypriceaverage", "avg_price"),
        migrations.RemoveField("ledgerentry", "price_tomans"),
        migrations.AddConstraint("price", models.CheckConstraint(
            condition=(
                models.Q(price_iranian__isnull=False, price_foreign__isnull=True)
                | models.Q(price_iranian__isnull=True, price_foreign__isnull=False)
            ), name="price_one_quote_storage",
        )),
        migrations.AddConstraint("dailypriceaverage", models.CheckConstraint(
            condition=(
                models.Q(avg_price_iranian__isnull=False, avg_price_foreign__isnull=True)
                | models.Q(avg_price_iranian__isnull=True, avg_price_foreign__isnull=False)
            ), name="daily_average_one_quote_storage",
        )),
        migrations.AddConstraint("ledgerentry", models.CheckConstraint(
            condition=(models.Q(price_iranian__isnull=True) | models.Q(price_foreign__isnull=True)),
            name="ledger_one_quote_storage",
        )),
    ]

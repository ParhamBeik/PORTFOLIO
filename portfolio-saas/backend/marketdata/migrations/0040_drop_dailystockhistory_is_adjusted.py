from django.db import migrations, models

# 0032_hypertables replaced this named CONSTRAINT with a same-named bare
# `CREATE UNIQUE INDEX` (never `ADD CONSTRAINT`), unconditionally, in every
# environment regardless of whether TimescaleDB is actually installed. Since
# then `uniq_stock_history_symbol_date_adj` has been an index, not a
# constraint, so Django's declarative RemoveConstraint (which issues
# `ALTER TABLE ... DROP CONSTRAINT`) fails with UndefinedObject -- this is
# the first migration to touch that name since 0032 introduced the drift.
_NAME = "uniq_stock_history_symbol_date_adj"
_TABLE = "marketdata_dailystockhistory"


def drop_old_unique(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{_NAME}"')
        cursor.execute(f'DROP INDEX IF EXISTS "{_NAME}"')


def restore_old_index(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f'CREATE UNIQUE INDEX "{_NAME}" ON {_TABLE} '
            f'(symbol, date, is_adjusted, ts)'
        )


def add_new_unique(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f'ALTER TABLE {_TABLE} ADD CONSTRAINT "{_NAME}" '
            f'UNIQUE (symbol, date, ts)'
        )


def drop_new_unique(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{_NAME}"')


class Migration(migrations.Migration):

    dependencies = [
        ('marketdata', '0039_market_snapshot_daily_bar'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(
                    model_name='dailystockhistory',
                    name=_NAME,
                ),
            ],
            database_operations=[
                migrations.RunPython(drop_old_unique, restore_old_index),
            ],
        ),
        migrations.RemoveField(
            model_name='dailystockhistory',
            name='is_adjusted',
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name='dailystockhistory',
                    constraint=models.UniqueConstraint(
                        fields=('symbol', 'date', 'ts'), name=_NAME,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunPython(add_new_unique, drop_new_unique),
            ],
        ),
    ]

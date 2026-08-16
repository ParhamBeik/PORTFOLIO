"""Make autoanalyze actually run on the bulk-append warehouse tables.

Postgres' default autovacuum_analyze_scale_factor is 0.10: a table is analyzed
only once 10% of its rows have changed. On an append-only table with tens of
millions of rows that threshold recedes as fast as the table grows, so it is
never crossed. The result was measured, not theorised: `last_analyze` was NULL
on every large table, and the planner believed `stocktransactiontick` held
916,470 rows when it held 41,654,840 -- a 45x error, 291x on
`dailystockhistory`. Every join and index decision on the warehouse was being
planned against that.

A flat row threshold with a zero scale factor makes the trigger absolute
("analyze after 50,000 new rows") instead of proportional, so it keeps firing at
any table size. 50,000 is roughly one trading day of ticks.

These are table storage parameters, not schema, so they live in RunSQL. The
reverse drops back to the cluster defaults rather than pinning 0.10, since the
default is what "unset" means.
"""
from django.db import migrations

# (table, analyze_threshold, also_tune_vacuum)
_TABLES = (
    ("marketdata_stocktransactiontick", 50000, True),
    ("marketdata_dailystockhistory", 50000, True),
    ("marketdata_marketcandle", 50000, True),
    ("marketdata_reallegalhistory", 50000, False),
    ("marketdata_goldcurrencyhistory", 10000, False),
    ("marketdata_workflowrun", 10000, False),
)


def _set_sql():
    statements = []
    for table, threshold, tune_vacuum in _TABLES:
        options = [
            "autovacuum_analyze_scale_factor = 0.0",
            f"autovacuum_analyze_threshold = {threshold}",
        ]
        if tune_vacuum:
            # Tick re-ingest deletes a (symbol, day) slice before re-inserting
            # it (marketdata/ingest.py), so these tables do accumulate dead
            # tuples and need the same absolute trigger for vacuum, not just
            # analyze.
            options += [
                "autovacuum_vacuum_scale_factor = 0.0",
                f"autovacuum_vacuum_threshold = {threshold}",
            ]
        statements.append(f"ALTER TABLE {table} SET ({', '.join(options)});")
    return "\n".join(statements)


def _reset_sql():
    names = (
        "autovacuum_analyze_scale_factor, autovacuum_analyze_threshold, "
        "autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold"
    )
    return "\n".join(
        f"ALTER TABLE {table} RESET ({names});" for table, _, _ in _TABLES
    )


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0027_archivefetchstate_blacklisted_and_more"),
    ]

    operations = [
        migrations.RunSQL(sql=_set_sql(), reverse_sql=_reset_sql()),
    ]

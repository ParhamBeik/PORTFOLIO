"""Delete backfill states for endpoints that only ever return a live snapshot.

`market_index_daily`, `etf_nav_daily` and `option_contract_daily` were seeded once
per symbol, but the provider serves all three as a single global snapshot that
cannot be asked for a past date -- `Index.php` ignores its `date` param entirely,
and `Nav.php` returns every ETF in one response and rejects a non-ETF `l18` with
502. The per-symbol fan-out could therefore never succeed; these rows are the
whole 400/404 error storm. `ensure_archive_states` no longer creates them.
"""
from django.db import migrations

DEAD_ENDPOINTS = ["market_index_daily", "etf_nav_daily", "option_contract_daily"]


def drop_live_states(apps, schema_editor):
    apps.get_model("marketdata", "ArchiveFetchState").objects.filter(
        endpoint__in=DEAD_ENDPOINTS
    ).delete()


class Migration(migrations.Migration):

    dependencies = [("marketdata", "0007_systemlogevent_service")]

    # Reverse is a no-op: ensure_archive_states reseeds whatever the current
    # endpoint classification says should exist, so recreating stale rows here
    # would only resurrect the fan-out this migration removes.
    operations = [migrations.RunPython(drop_live_states, migrations.RunPython.noop)]

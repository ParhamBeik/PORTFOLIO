"""Compress tick chunks after 7 days instead of 30 (docs/STORAGE-POLICY.md).

Compression is lossless: every row stays queryable, and TimescaleDB 2.11+ still
accepts inserts into compressed chunks, so the archive backfill keeps working.
"""

from django.db import migrations

_TICK = "marketdata_stocktransactiontick"


def _set_policy(days):
    def run(apps, schema_editor):
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')")
            if not cursor.fetchone()[0]:
                return
            cursor.execute(f"SELECT remove_compression_policy('{_TICK}', if_exists => true)")
            cursor.execute(
                f"SELECT add_compression_policy('{_TICK}', INTERVAL '{days} days', "
                f"if_not_exists => true)")
    return run


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0019_codal_history_window")]
    operations = [migrations.RunPython(_set_policy(7), _set_policy(30))]

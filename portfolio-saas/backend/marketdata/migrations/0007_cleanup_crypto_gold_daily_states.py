"""Remove invalid gold_daily archive fetch states for non-gold/currency instruments
and standardize manual asset price sources.

Previously, `tracked_brs_symbols()` fetched all eligible BRS symbols without
filtering by category, creating `gold_daily` fetch states for 1,200+ crypto tokens.
When backfilled against BRS gold endpoints, these failed with HTTP 400.

This data migration deletes those misassigned states so only genuine gold and
currency symbols remain on the gold_daily archive loop, and updates older manual
asset prices with source='API' to source='MANUAL'.
"""
from django.db import migrations


def cleanup_misassigned_states(apps, schema_editor):
    ArchiveFetchState = apps.get_model("marketdata", "ArchiveFetchState")
    MarketInstrument = apps.get_model("marketdata", "MarketInstrument")
    Asset = apps.get_model("portfolio", "Asset")
    Price = apps.get_model("portfolio", "Price")

    active_asset_symbols = set(
        Asset.objects.exclude(brs_symbol="").values_list("brs_symbol", flat=True)
    )
    non_gold_symbols = set(
        MarketInstrument.objects.filter(source="brs")
        .exclude(category__in=("gold", "currency"))
        .values_list("symbol", flat=True)
    )
    symbols_to_remove = non_gold_symbols - active_asset_symbols

    if symbols_to_remove:
        ArchiveFetchState.objects.filter(
            endpoint="gold_daily",
            symbol__in=symbols_to_remove,
        ).delete()

    # Update older fallback price rows for manual assets from API to MANUAL
    manual_asset_ids = list(Asset.objects.filter(is_manual=True).values_list("id", flat=True))
    if manual_asset_ids:
        Price.objects.filter(asset_id__in=manual_asset_ids, source="API").update(source="MANUAL")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("marketdata", "0006_clamp_tick_windows"),
        ("portfolio", "0032_rights_issue_kind"),
    ]

    operations = [
        migrations.RunPython(cleanup_misassigned_states, noop),
    ]

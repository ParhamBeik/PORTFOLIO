from decimal import Decimal

from portfolio.models import DailyPriceAverage, Price
from portfolio.tasks import aggregate_daily_price_averages


def test_aggregate_excludes_archive_rows_from_the_average(asset_catalog):
    """ARCHIVE-tagged Price rows are guard_price_map's forward-fill-to-warehouse
    writes, not a live observation -- they must not enter the daily average.
    """
    asset = asset_catalog["emami_coin"]
    Price.objects.create(asset=asset, price=Decimal("100"), source="API")
    Price.objects.create(asset=asset, price=Decimal("200"), source="API")
    Price.objects.create(asset=asset, price=Decimal("999999"), source="ARCHIVE")

    written = aggregate_daily_price_averages()

    assert written == 1
    row = DailyPriceAverage.objects.get(asset=asset)
    assert row.avg_price == Decimal("150.0000")
    assert row.sample_count == 2


def test_aggregate_skips_assets_with_no_ticks(asset_catalog):
    written = aggregate_daily_price_averages()

    assert written == 0
    assert not DailyPriceAverage.objects.exists()


def test_aggregate_is_idempotent_per_day(asset_catalog):
    asset = asset_catalog["emami_coin"]
    Price.objects.create(asset=asset, price=Decimal("100"), source="API")

    aggregate_daily_price_averages()
    aggregate_daily_price_averages()

    assert DailyPriceAverage.objects.filter(asset=asset).count() == 1

from django.core.management import call_command

from marketdata.models import MarketInstrument
from portfolio.models import Asset


def test_seed_assets_includes_formula_valued_house(db):
    MarketInstrument.objects.bulk_create([
        MarketInstrument(
            source="brs",
            symbol=symbol,
            category=MarketInstrument.Category.GOLD,
            eligible=True,
        )
        for symbol in (
            "IR_COIN_EMAMI",
            "IR_COIN_HALF",
            "IR_COIN_QUARTER",
            "IR_COIN_1G",
            "IR_GOLD_18K",
            "USD",
        )
    ] + [
        MarketInstrument(
            source="tsetmc",
            symbol="کاما",
            category=MarketInstrument.Category.STOCK,
            eligible=True,
        )
    ])

    call_command("seed_assets")
    Asset.objects.filter(key="house_asset").update(is_active=False)
    call_command("seed_assets")

    house = Asset.objects.get(key="house_asset")
    assert house.is_active
    assert house.is_house
    assert house.asset_class == Asset.AssetClass.REAL_ESTATE

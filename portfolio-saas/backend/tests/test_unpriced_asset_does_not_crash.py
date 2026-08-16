"""An unpriced holding must not take down the analytics endpoints.

Unit tests: `_liquid_items` / `_total` are pure functions over the valuation
payload dict, so the defect (a None value reaching sum()/max()) reproduces
without a database. That keeps this fast and pins exactly one behaviour.
"""
from decimal import Decimal

from portfolio.services.insights import (
    _liquid_items,
    _total,
    allocation_breakdown,
    concentration_risk,
)


def _valuation_with_one_unpriced():
    """Shape produced by value_user(): one priced asset, one with no price."""
    return {
        "accounts": [
            {
                "items": [
                    {
                        "key": "emami_coin",
                        "asset": "Emami Coin",
                        "class": "Gold",
                        "value": Decimal("1000"),
                        "quality_status": "live",
                    },
                    {
                        # valuation.py sets value=None when the price is missing.
                        "key": "kama_stock",
                        "asset": "KAMA Stock",
                        "class": "Stock",
                        "value": None,
                        "quality_status": "unavailable",
                    },
                    {
                        "key": "house_asset",
                        "asset": "Real Estate",
                        "class": "Real Estate",
                        "value": Decimal("5000"),
                        "quality_status": "manual",
                    },
                ]
            }
        ],
        "excluded": [{"asset_key": "kama_stock", "reason": "missing_price"}],
    }


def test_unpriced_holding_is_dropped_from_liquid_items():
    items = _liquid_items(_valuation_with_one_unpriced())

    keys = {i["key"] for i in items}
    assert keys == {"emami_coin"}  # real estate and the unpriced stock both gone
    assert all(i["value"] is not None for i in items)


def test_total_does_not_raise_on_an_unpriced_holding():
    # Before the fix this raised TypeError: unsupported operand Decimal + None.
    assert _total(_liquid_items(_valuation_with_one_unpriced())) == Decimal("1000")


def test_allocation_and_concentration_survive_an_unpriced_holding():
    valuation = _valuation_with_one_unpriced()

    # concentration_risk did `max(items, key=lambda i: i["value"])`, which raised.
    breakdown = allocation_breakdown(valuation)
    risk = concentration_risk(valuation)

    # The one priced liquid asset is 100% of the liquid portfolio; the unpriced
    # stock contributes nothing rather than blowing up the calculation.
    assert breakdown == {"Gold": 100.0}
    assert risk["share"] == 100.0  # concentration_risk reports a percentage


def test_every_holding_unpriced_yields_empty_not_an_exception():
    valuation = {"accounts": [{"items": [
        {"key": "a", "asset": "A", "class": "Gold", "value": None,
         "quality_status": "unavailable"},
    ]}]}

    assert _liquid_items(valuation) == []
    assert _total(_liquid_items(valuation)) == Decimal("0")
    assert concentration_risk(valuation)["severity"] == "info"

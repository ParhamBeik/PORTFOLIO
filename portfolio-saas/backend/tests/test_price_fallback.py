from decimal import Decimal

from portfolio.tasks import _persistable_prices


def test_archive_replacement_is_persisted_but_forward_fill_is_not():
    priced, sources = _persistable_prices(
        {"archive": 0, "forward_fill": 0, "live": 4300},
        {
            "archive": Decimal("4360"),
            "forward_fill": Decimal("4240"),
            "live": Decimal("4300"),
        },
        {"archive": Decimal("4360")},
    )

    assert priced == {"archive": Decimal("4360"), "live": Decimal("4300")}
    assert sources == {"archive": "ARCHIVE", "live": "API"}

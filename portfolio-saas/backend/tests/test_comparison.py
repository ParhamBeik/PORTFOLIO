"""Counterfactual comparison: what the same money would have done elsewhere.

Integration tests, because the question spans the ledger (real dates and
amounts), the Toman price panel (three warehouse tables and a unit boundary)
and the API's reason codes. A unit test of the arithmetic alone would pass
while the panel handed it Rial.
"""
import datetime

import pytest
from decimal import Decimal
from django.utils import timezone
from rest_framework.test import APIClient

from portfolio.models import Account, LedgerEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def compared(asset_catalog, make_user):
    """A stock bought in two lots, and a gold series to have bought instead."""
    from marketdata.models import GoldCurrencyHistory, MarketCandle

    user = make_user(email="comparison@test.test")
    account = Account.objects.create(user=user, name="Broker")
    start = timezone.now() - datetime.timedelta(days=60)

    # Stock doubles over the window; gold quadruples.
    for offset in range(61):
        day = (start + datetime.timedelta(days=offset)).date()
        jalali = _jalali(day)
        MarketCandle.objects.create(
            symbol="کاما", timeframe=MarketCandle.ADJUSTED, date_time=jalali,
            close_price=Decimal(1000 + offset * 1000 // 60),
            open_price=Decimal("1000"), high_price=Decimal("2000"),
            low_price=Decimal("1000"), volume=1,
        )
        GoldCurrencyHistory.objects.create(
            symbol="IR_GOLD_18K", date=jalali, unit="تومان",
            close_price=Decimal(1000 + offset * 3000 // 60),
        )
    asset_catalog["gold_18k_gram"].brs_symbol = "IR_GOLD_18K"
    asset_catalog["gold_18k_gram"].save(update_fields=["brs_symbol"])

    from portfolio.services.ledger import create_ledger_entry

    # Bought at the market price of each day, so the second lot costs more per
    # share -- which is the only thing that makes the drip and the lump sum
    # differ at all.
    for offset, price in ((0, "1000"), (30, "1500")):
        create_ledger_entry(
            account=account, kind=LedgerEntry.Kind.BUY,
            asset=asset_catalog["kama_stock"], quantity=Decimal("1000"),
            unit_price_tomans=Decimal(price),
            occurred_at=start + datetime.timedelta(days=offset),
        )
    return account


def _jalali(day):
    import jdatetime

    return jdatetime.date.fromgregorian(date=day).strftime("%Y-%m-%d")


def _get(account, **params):
    client = APIClient()
    client.force_authenticate(user=account.user)
    query = "&".join(f"{k}={v}" for k, v in {"account": account.id, **params}.items())
    return client.get(f"/api/comparison/?{query}")


def test_the_picker_separates_what_you_hold_from_what_you_could_hold(compared):
    response = _get(compared)

    assert response.status_code == 200, response.data
    assert [row["key"] for row in response.data["holdings"]] == ["kama_stock"]
    targets = {row["key"] for row in response.data["targets"]}
    assert "gold_18k_gram" in targets
    # Property is valued from the owner's own marks, so it can never be an
    # investment alternative.
    assert "house_asset" not in targets


def test_the_same_money_in_gold_is_worth_what_gold_did_with_it(compared):
    response = _get(
        compared, mode="counterfactual", subject="kama_stock", target="gold_18k_gram"
    )

    assert response.status_code == 200, response.data
    actual, alternative = response.data["series"]
    assert actual["key"] == "kama_stock"
    assert alternative["key"] == "gold_18k_gram"
    summary = response.data["summary"]
    # 1,000 x 1,000 Rial then 1,000 x 1,500 Rial = 2,500,000 Rial = 250,000 Toman.
    assert summary["invested_tomans"] == pytest.approx(250000, rel=1e-6)
    # Gold ran away from the stock over this window, so the road not taken wins.
    assert summary["alternative_end_tomans"] > summary["actual_end_tomans"]
    assert summary["difference_tomans"] < 0
    assert len(actual["points"]) == len(alternative["points"]) > 1


def test_a_lump_sum_beats_the_drip_when_the_price_only_rises(compared):
    """Same total, all on day one. On a monotonically rising series that must
    beat buying the second half a month later -- if it does not, the drip's
    dates are not being honoured.
    """
    drip = _get(
        compared, mode="counterfactual", subject="kama_stock", target="gold_18k_gram"
    ).data
    lump = _get(
        compared, mode="lump_sum", subject="kama_stock", target="gold_18k_gram"
    ).data

    assert lump["summary"]["invested_tomans"] == drip["summary"]["invested_tomans"]
    assert lump["summary"]["actual_end_tomans"] > drip["summary"]["actual_end_tomans"]


def test_a_benchmark_run_is_rebased_not_added_up(compared):
    response = _get(compared, mode="benchmark", target="gold_18k_gram")

    assert response.status_code == 200, response.data
    portfolio, gold = response.data["series"]
    assert portfolio["unit"] == gold["unit"] == "index"
    # Both start at 100 by construction; comparing levels is the whole point of
    # rebasing, since a portfolio total and one gram of gold share no scale.
    assert portfolio["points"][0]["value"] == pytest.approx(100)
    assert gold["points"][0]["value"] == pytest.approx(100)


def test_property_is_refused_with_a_reason_not_a_zero(compared):
    response = _get(
        compared, mode="counterfactual", subject="kama_stock", target="house_asset"
    )

    assert response.status_code == 400
    assert response.data["reason"] == "real_estate_not_comparable"


def test_a_position_with_no_recorded_price_has_no_money_to_move(
    compared, asset_catalog
):
    """An opening position states what you hold, not what you paid. Replaying
    it as a purchase would hand the alternative money that was never spent.
    """
    other = Account.objects.create(user=compared.user, name="Inherited")
    from portfolio.services.ledger import create_ledger_entry

    create_ledger_entry(
        account=other, kind=LedgerEntry.Kind.OPENING_POSITION,
        asset=asset_catalog["kama_stock"], quantity=Decimal("500"),
        occurred_at=timezone.now() - datetime.timedelta(days=40),
    )
    client = APIClient()
    client.force_authenticate(user=compared.user)
    response = client.get(
        f"/api/comparison/?account={other.id}&mode=counterfactual"
        "&subject=kama_stock&target=gold_18k_gram"
    )

    assert response.status_code == 400
    assert response.data["reason"] == "no_recorded_cost"


def test_comparing_an_asset_with_itself_is_refused(compared):
    response = _get(
        compared, mode="counterfactual", subject="kama_stock", target="kama_stock"
    )

    assert response.status_code == 400
    assert response.data["reason"] == "same_asset"


def test_a_short_window_still_prices_an_older_purchase_at_its_own_day(compared):
    """The window says how much to LOOK at, never how far back to replay. A
    30-day view of a 60-day-old purchase must still buy the alternative at the
    price on the day the money was actually spent -- buying it at the window's
    opening price instead would silently rewrite the cost.
    """
    full = _get(
        compared, mode="counterfactual", subject="kama_stock",
        target="gold_18k_gram", days=0,
    ).data
    short = _get(
        compared, mode="counterfactual", subject="kama_stock",
        target="gold_18k_gram", days=30,
    ).data

    assert len(short["series"][0]["points"]) < len(full["series"][0]["points"])
    # Same money, same replay -- only the visible slice differs, so today's
    # value of the alternative is identical either way.
    assert short["summary"]["alternative_end_tomans"] == pytest.approx(
        full["summary"]["alternative_end_tomans"]
    )
    assert short["summary"]["invested_tomans"] == full["summary"]["invested_tomans"]

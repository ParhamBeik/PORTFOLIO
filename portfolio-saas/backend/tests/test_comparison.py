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


def test_both_modes_report_the_same_amount_invested(compared, asset_catalog):
    """A sale returns money, so it reduces what you put in. Counting gross buys
    in one mode and net in the other made the same portfolio report two
    different totals depending on which tab was open.
    """
    from portfolio.services.ledger import create_ledger_entry

    create_ledger_entry(
        account=compared, kind=LedgerEntry.Kind.SELL,
        asset=asset_catalog["kama_stock"], quantity=Decimal("500"),
        unit_price_tomans=Decimal("2000"),
        occurred_at=timezone.now() - datetime.timedelta(days=5),
    )
    drip = _get(
        compared, mode="counterfactual", subject="kama_stock", target="gold_18k_gram"
    ).data
    lump = _get(
        compared, mode="lump_sum", subject="kama_stock", target="gold_18k_gram"
    ).data

    # 250,000 put in, then 500 x 2,000 Rial = 100,000 Toman taken back out.
    assert drip["summary"]["invested_tomans"] == pytest.approx(150000, rel=1e-6)
    assert lump["summary"]["invested_tomans"] == drip["summary"]["invested_tomans"]


def test_a_benchmark_window_longer_than_the_replay_says_so(compared):
    """The net-worth series is capped, so "All" cannot mean all. Saying nothing
    would let the axis imply a range the data does not cover.
    """
    from portfolio.services.valuation import SYNTHETIC_HISTORY_MAX_DAYS

    capped = _get(
        compared, mode="benchmark", target="gold_18k_gram",
        days=SYNTHETIC_HISTORY_MAX_DAYS + 200,
    ).data
    within = _get(
        compared, mode="benchmark", target="gold_18k_gram", days=30
    ).data

    assert capped["summary"]["truncated_to_days"] == SYNTHETIC_HISTORY_MAX_DAYS
    assert within["summary"]["truncated_to_days"] is None


def test_a_dollar_quoted_target_is_not_reported_as_toman(compared, asset_catalog):
    """`_load_price_panel` leaves USD_QUOTED_KEYS in dollars -- the conversion
    runs later, inside daily_returns_matrix. Every other consumer takes
    pct_change() next, where a constant factor cancels, so nothing noticed. This
    page prints the number, so it must read the converted panel.
    """
    import jdatetime
    from marketdata.models import GoldCurrencyHistory
    from portfolio.models import Price

    coin = asset_catalog["bitcoin_usd"]
    coin.brs_symbol = ""
    coin.save(update_fields=["brs_symbol"])
    start = timezone.now() - datetime.timedelta(days=60)
    for offset in range(61):
        day = (start + datetime.timedelta(days=offset)).date()
        jalali = _jalali(day)
        GoldCurrencyHistory.objects.create(
            symbol="USD", date=jalali, unit="تومان", close_price=Decimal("100000"),
        )
        Price.objects.create(
            asset=coin, price=Decimal("1000"), source="TEST",
            fetched_at=timezone.now() - datetime.timedelta(days=60 - offset),
        )
    asset_catalog["usd_cash"].brs_symbol = "USD"
    asset_catalog["usd_cash"].save(update_fields=["brs_symbol"])

    response = _get(
        compared, mode="counterfactual", subject="kama_stock", target="bitcoin_usd"
    )
    if response.status_code != 200:
        # An honest refusal is acceptable; silently pricing dollars as Toman
        # is not, which is what the assertion below is really guarding.
        assert response.data["reason"] in {
            "missing_price_history", "stale_price_history",
            "history_starts_after_purchase",
        }
        return
    end = response.data["summary"]["alternative_end_tomans"]
    invested = response.data["summary"]["invested_tomans"]
    # At 1,000 dollars a coin and 100,000 Toman a dollar, 250,000 Toman buys
    # 0.0025 of one. Read as Toman it would buy 250 -- a factor of 100,000.
    assert end == pytest.approx(invested, rel=0.01)


def test_a_target_whose_price_series_stopped_is_refused(compared, asset_catalog):
    """A halted or delisted target used to draw a flat line from its last close
    to today, and the summary reported that stale number as what you would have
    made. This is the forward-fill bound the whole codebase is built around.
    """
    from marketdata.models import GoldCurrencyHistory

    dead = asset_catalog["euro_cash"]
    dead.brs_symbol = "EUR"
    dead.save(update_fields=["brs_symbol"])
    # Priced daily for a month, then nothing for the last 12 days -- inside the
    # 21-calendar-day drawing fill, outside the 5-session staleness bound. The
    # earlier 40-day gap passed under either, so nothing pinned the difference.
    start = timezone.now() - datetime.timedelta(days=42)
    for offset in range(31):
        day = (start + datetime.timedelta(days=offset)).date()
        GoldCurrencyHistory.objects.create(
            symbol="EUR", date=_jalali(day), unit="تومان",
            close_price=Decimal("50000"),
        )

    response = _get(
        compared, mode="counterfactual", subject="kama_stock", target="euro_cash"
    )

    assert response.status_code == 400, response.data
    assert response.data["reason"] == "stale_price_history"


# ----------------------------------------------------------------------
# Time-weighted index: money arriving is not money earned.
#
# `_twr_index` is pure arithmetic over the series dicts, so these are unit tests
# despite the module-level django_db mark -- the whole failure mode lives in the
# chaining rule and needs no warehouse to reproduce.


def _point(date, total, ex_flows):
    return {"date": date, "total": str(total), "total_ex_flows": str(ex_flows)}


def test_twr_index_tracks_price_moves_when_the_book_does_not_change():
    from portfolio.services.comparison import _twr_index

    # No quantity ever changes, so each day is already valued at yesterday's
    # book and the index must follow the totals exactly: 100 -> 110 -> 121.
    series = [
        _point("2026-01-01", 1000, 1000),
        _point("2026-01-02", 1100, 1100),
        _point("2026-01-03", 1210, 1210),
    ]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 110.0, 121.0])


def test_recording_a_position_you_already_owned_is_not_a_gain():
    from portfolio.services.comparison import _twr_index

    # Day 2 doubles the total, but every Toman of it arrived as a book entry:
    # valued at yesterday's quantities the day is worth exactly yesterday's
    # 1000. This is the 2026-08-09 shape that read as +108% in one day.
    series = [
        _point("2026-01-01", 1000, 1000),
        _point("2026-01-02", 2000, 1000),
        _point("2026-01-03", 2200, 2200),
    ]
    index = _twr_index(series)

    # Flat across the opening, then the real +10% price move on day 3.
    assert list(index) == pytest.approx([100.0, 100.0, 110.0])
    assert float(index.iloc[-1]) - 100 == pytest.approx(10.0)


def test_twr_index_survives_a_zero_starting_day_without_dividing_by_it():
    from portfolio.services.comparison import _twr_index

    series = [
        _point("2026-01-01", 0, 0),
        _point("2026-01-02", 500, 0),
        _point("2026-01-03", 550, 550),
    ]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 100.0, 110.0])


def test_twr_index_falls_back_to_total_when_the_companion_figure_is_absent():
    from portfolio.services.comparison import _twr_index

    series = [{"date": "2026-01-01", "total": "1000"}, {"date": "2026-01-02", "total": "1100"}]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 110.0])


# ----------------------------------------------------------------------
# Time-weighted return: a bookkeeping entry is not a gain.
#
# `compute_dynamic_net_worth_series` walks quantities backwards through the
# ledger, so the day an "already owned" position is first recorded, net worth
# steps up by the whole position. Rebasing that raw series to 100 read the step
# as performance: the family account's openings on 2026-08-09 showed as a +108%
# day and reported +120.7% for a quarter in which the portfolio grew 40.4%.
#
# Unit test: pure arithmetic over a small list of dicts, no DB and no prices, so
# the chain-linking is pinned directly where the error was.


def _point(date, total, ex_flows):
    return {"date": date, "total": str(total), "total_ex_flows": str(ex_flows)}


def test_twr_ignores_a_position_being_recorded_for_the_first_time():
    from portfolio.services.comparison import _twr_index

    series = [
        _point("2026-08-06", 100, 100),
        # Prices up 10%; the book did not change.
        _point("2026-08-07", 110, 110),
        # The book DOUBLES because an already-owned position was written down.
        # Priced at yesterday's quantities the day was flat, so it earned nothing.
        _point("2026-08-08", 220, 110),
        # Prices up 10% again, now on the larger book.
        _point("2026-08-09", 242, 242),
    ]

    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 110.0, 110.0, 121.0], abs=1e-9)
    # 1.10 * 1.00 * 1.10 - 1 = 21%, not the 142% the raw totals imply.
    assert float(index.iloc[-1]) - 100 == pytest.approx(21.0, abs=1e-9)


def test_twr_still_counts_real_price_moves():
    from portfolio.services.comparison import _twr_index

    series = [
        _point("2026-08-06", 100, 100),
        _point("2026-08-07", 150, 150),
    ]
    index = _twr_index(series)
    assert float(index.iloc[-1]) == pytest.approx(150.0, abs=1e-9)


def test_twr_restarts_the_chain_instead_of_dividing_by_an_empty_book():
    from portfolio.services.comparison import _twr_index

    # An empty portfolio cannot carry a return; the next day must not divide by 0.
    series = [
        _point("2026-08-06", 0, 0),
        _point("2026-08-07", 500, 0),
        _point("2026-08-08", 550, 550),
    ]
    index = _twr_index(series)
    assert float(index.iloc[0]) == pytest.approx(100.0, abs=1e-9)
    assert float(index.iloc[1]) == pytest.approx(100.0, abs=1e-9)
    # Only the genuine 10% move after the book existed is counted.
    assert float(index.iloc[2]) == pytest.approx(110.0, abs=1e-9)


def test_twr_falls_back_to_total_when_the_companion_figure_is_absent():
    from portfolio.services.comparison import _twr_index

    series = [{"date": "2026-08-06", "total": "100"}, {"date": "2026-08-07", "total": "110"}]
    index = _twr_index(series)
    assert float(index.iloc[-1]) == pytest.approx(110.0, abs=1e-9)

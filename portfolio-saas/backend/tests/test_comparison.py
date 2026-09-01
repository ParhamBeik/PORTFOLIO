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
    from portfolio.services.comparison import BENCHMARK_MAX_DAYS

    capped = _get(
        compared, mode="benchmark", target="gold_18k_gram",
        days=BENCHMARK_MAX_DAYS + 200,
    ).data
    within = _get(
        compared, mode="benchmark", target="gold_18k_gram", days=30
    ).data

    assert capped["summary"]["truncated_to_days"] == BENCHMARK_MAX_DAYS
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
            "no_overlapping_history",
        }
        return
    end = response.data["summary"]["alternative_end_tomans"]
    invested = response.data["summary"]["invested_tomans"]
    # At 1,000 dollars a coin and 100,000 Toman a dollar, 250,000 Toman buys
    # 0.0025 of one. Read as Toman it would buy 250 -- a factor of 100,000.
    assert end == pytest.approx(invested, rel=0.01)


def test_a_target_whose_price_series_stopped_ends_the_window(compared, asset_catalog):
    """A halted or delisted target used to draw a flat line from its last close
    to today, and the summary reported that stale number as what you would have
    made. That is what the forward-fill bound exists to prevent -- but refusing
    the whole comparison was a heavier answer than the bound requires. The
    window now STOPS at the last day both series really traded, which reports no
    stale price as today's outcome while still answering the question.
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
    last_real = (start + datetime.timedelta(days=30)).date()

    response = _get(
        compared, mode="counterfactual", subject="kama_stock", target="euro_cash"
    )

    assert response.status_code == 200, response.data
    # The whole point: the window ends where the euro's data ends, not today.
    assert response.data["summary"]["end_date"] == last_real.isoformat()
    assert last_real < timezone.now().date()
    ended = [
        w for w in response.data["warnings"]
        if w["key"] == "euro_cash" and w["reason"] == "series_ended"
    ]
    assert ended, response.data["warnings"]
    assert last_real.isoformat() in ended[0]["detail"]
    # And no curve runs past that day either -- a chart drawn to today with a
    # flat tail is the same lie in a different place.
    for curve in response.data["series"]:
        assert curve["points"][-1]["date"] == last_real.isoformat()


# ----------------------------------------------------------------------
# Partial overlap and the range buttons.
#
# Integration tests again: the whole failure being pinned here is the seam
# between what the warehouse happens to hold and what the page decides to say,
# and neither half reproduces it alone.


@pytest.fixture
def late_target(compared, asset_catalog):
    """A target the warehouse only started carrying three weeks ago.

    The reported case: a position bought two months back, compared against
    something whose price series is much younger. Every such pair used to
    answer "no data at all".
    """
    from marketdata.models import GoldCurrencyHistory

    coin = asset_catalog["emami_coin"]
    coin.brs_symbol = "IR_COIN_EMAMI"
    coin.save(update_fields=["brs_symbol"])
    start = timezone.now() - datetime.timedelta(days=19)
    for offset in range(20):
        day = (start + datetime.timedelta(days=offset)).date()
        GoldCurrencyHistory.objects.create(
            symbol="IR_COIN_EMAMI", date=_jalali(day), unit="تومان",
            close_price=Decimal(500000 + offset * 10000),
        )
    return compared, start.date()


@pytest.fixture
def two_holdings(late_target, asset_catalog):
    """Both assets really held, their histories 40 days apart in length."""
    from portfolio.services.ledger import create_ledger_entry

    account, first_shared = late_target
    create_ledger_entry(
        account=account, kind=LedgerEntry.Kind.BUY,
        asset=asset_catalog["emami_coin"], quantity=Decimal("1"),
        unit_price_tomans=Decimal("480000"),
        occurred_at=timezone.now() - datetime.timedelta(days=40),
    )
    return account, first_shared


def test_a_target_with_a_shorter_history_starts_where_the_data_starts(late_target):
    """The headline fix: compare from the first day both series exist, and say
    so, instead of refusing because one of them does not reach back far enough.
    """
    account, first_shared = late_target

    data = _get(
        account, mode="counterfactual", subject="kama_stock",
        target="emami_coin", days=0,
    ).data

    assert data["summary"]["start_date"] == first_shared.isoformat()
    # Both sides start from the same real stake -- what the position was
    # actually worth that day -- so the gap between them is performance and not
    # an artefact of one curve starting at zero.
    assert data["summary"]["carried_in_tomans"] > 0
    assert data["summary"]["actual_end_tomans"] > 0
    assert data["summary"]["alternative_end_tomans"] > 0
    note = [w for w in data["warnings"] if w["reason"] == "history_starts_late"]
    assert note, data["warnings"]
    assert first_shared.isoformat() in note[0]["detail"]


def test_all_at_once_answers_against_a_late_target_too(late_target):
    account, first_shared = late_target

    data = _get(
        account, mode="lump_sum", subject="kama_stock", target="emami_coin", days=0,
    ).data

    assert data["summary"]["start_date"] == first_shared.isoformat()
    assert data["summary"]["alternative_end_tomans"] > 0


def test_two_of_mine_loads_back_to_the_older_position(two_holdings):
    """The pair's window is the EARLIER of the two openings. Reading the first
    flow of the concatenated list instead loaded a panel that stopped short of
    whichever asset happened to be named second.
    """
    account, first_shared = two_holdings

    data = _get(
        account, mode="holdings", subject="emami_coin", target="kama_stock", days=0,
    ).data

    assert data["summary"]["start_date"] == first_shared.isoformat()
    # The stock was bought 60 days ago and the coin 40; on the first shared day
    # both are already held, so neither curve opens at zero.
    for curve in data["series"]:
        assert curve["points"][0]["value"] > 0


@pytest.mark.parametrize("mode", ["counterfactual", "holdings", "benchmark", "lump_sum"])
@pytest.mark.parametrize("days", [0, 365, 180, 90])
def test_every_mode_and_range_answers_with_a_verdict_or_a_reason(
    two_holdings, mode, days
):
    """Sixteen combinations, one rule: never a silent empty panel, and never a
    window longer than the one asked for.
    """
    account, _ = two_holdings
    params = {"mode": mode, "target": "gold_18k_gram", "days": days}
    if mode != "benchmark":
        params["subject"] = "kama_stock"

    response = _get(account, **params)

    if response.status_code != 200:
        assert response.data.get("reason"), response.data
        return
    summary = response.data["summary"]
    assert response.data["series"][0]["points"], summary
    assert summary["start_date"] and summary["end_date"]
    if days:
        # A day of slack: the window is inclusive of both endpoints.
        assert summary["window_days"] <= days + 1, summary


@pytest.fixture
def long_history(asset_catalog, make_user):
    """A portfolio and a target that both go back well over a year."""
    from marketdata.models import GoldCurrencyHistory, MarketCandle
    from portfolio.services.ledger import create_ledger_entry

    user = make_user(email="longrun@test.test")
    account = Account.objects.create(user=user, name="Broker")
    start = timezone.now() - datetime.timedelta(days=420)
    for offset in range(421):
        day = (start + datetime.timedelta(days=offset)).date()
        jalali = _jalali(day)
        MarketCandle.objects.create(
            symbol="کاما", timeframe=MarketCandle.ADJUSTED, date_time=jalali,
            close_price=Decimal(10000 + offset * 10),
            open_price=Decimal("10000"), high_price=Decimal("20000"),
            low_price=Decimal("10000"), volume=1,
        )
        GoldCurrencyHistory.objects.create(
            symbol="IR_GOLD_18K", date=jalali, unit="تومان",
            close_price=Decimal(1000 + offset * 5),
        )
    asset_catalog["gold_18k_gram"].brs_symbol = "IR_GOLD_18K"
    asset_catalog["gold_18k_gram"].save(update_fields=["brs_symbol"])
    create_ledger_entry(
        account=account, kind=LedgerEntry.Kind.BUY,
        asset=asset_catalog["kama_stock"], quantity=Decimal("1000"),
        unit_price_tomans=Decimal("1000"),
        occurred_at=start + datetime.timedelta(days=1),
    )
    return account


def test_the_benchmark_ranges_are_three_different_windows(long_history):
    """1Y, 6M and 90D returned the identical ninety days, because the portfolio
    replay behind this tab borrowed a constant meant for something else. Three
    buttons, one answer, no error -- the failure nobody reports.
    """
    windows = {
        days: _get(
            long_history, mode="benchmark", target="gold_18k_gram", days=days
        ).data["summary"]["window_days"]
        for days in (365, 180, 90)
    }

    assert len(set(windows.values())) == 3, windows
    assert windows[365] > windows[180] > windows[90]
    # And each is the window that was asked for, not merely a different one.
    for days, window in windows.items():
        assert abs(window - days) <= 1, windows


def test_a_benchmark_window_does_not_open_before_the_portfolio_did(compared):
    """A year of a flat 100 line ahead of the first purchase is not history."""
    data = _get(compared, mode="benchmark", target="gold_18k_gram", days=365).data

    assert data["summary"]["window_days"] <= 65, data["summary"]


# ----------------------------------------------------------------------
# Time-weighted index: money arriving is not money earned.
#
# `_twr_index` is pure arithmetic over the series dicts, so these are unit tests
# despite the module-level django_db mark -- the whole failure mode lives in the
# chaining rule and needs no warehouse to reproduce.


def _point(date, total, ex_flows, ex_base=None):
    """`ex_base` is the same book at the PREVIOUS day's prices; default: flat."""
    return {
        "date": date,
        "total": str(total),
        "total_ex_flows": str(ex_flows),
        "total_ex_flows_base": str(ex_flows if ex_base is None else ex_base),
    }


def test_twr_index_tracks_price_moves_when_the_book_does_not_change():
    from portfolio.services.comparison import _twr_index

    # Nothing is bought or sold, so each day's pair is just yesterday's holdings
    # at today's price over the same holdings at yesterday's price: +10%, +10%.
    series = [
        _point("2026-01-01", 1000, 1000, 1000),
        _point("2026-01-02", 1100, 1100, 1000),
        _point("2026-01-03", 1210, 1210, 1100),
    ]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 110.0, 121.0])


def test_recording_a_position_you_already_owned_is_not_a_gain():
    from portfolio.services.comparison import _twr_index

    # Day 2 doubles the total, but every Toman of it arrived as a book entry:
    # priced at yesterday's quantities the day was flat. This is the 2026-08-09
    # shape that read as +108% in one day.
    series = [
        _point("2026-01-01", 1000, 1000, 1000),
        _point("2026-01-02", 2000, 1000, 1000),
        _point("2026-01-03", 2200, 2200, 2000),
    ]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 100.0, 110.0])
    assert float(index.iloc[-1]) - 100 == pytest.approx(10.0)


def test_an_asset_dropped_for_a_price_gap_does_not_book_a_loss():
    from portfolio.services.comparison import _twr_index

    # Day 2 loses a holding to the forward-fill guard: it is absent from BOTH
    # sides of the pair, so the day reads flat on what remains. Dividing by the
    # previous day's full total instead subtracted that holding's whole weight
    # and, the index being a running product, never gave it back.
    series = [
        _point("2026-01-01", 1000, 1000, 1000),
        _point("2026-01-02", 600, 400, 400),
        _point("2026-01-03", 660, 660, 600),
    ]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 100.0, 110.0])


def test_twr_index_survives_a_zero_starting_day_without_dividing_by_it():
    from portfolio.services.comparison import _twr_index

    series = [
        _point("2026-01-01", 0, 0, 0),
        _point("2026-01-02", 500, 0, 0),
        _point("2026-01-03", 550, 550, 500),
    ]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 100.0, 110.0])


def test_twr_index_falls_back_to_total_when_the_companion_figure_is_absent():
    from portfolio.services.comparison import _twr_index

    # A payload from before the fix: no pair to work with, so the old curve is
    # reproduced rather than raising on a key that is merely missing.
    series = [{"date": "2026-01-01", "total": "1000"}, {"date": "2026-01-02", "total": "1100"}]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 110.0])


def test_a_mortgage_is_carried_on_both_sides_so_leverage_shows():
    from portfolio.services.comparison import _twr_index

    # Assets 1000 against a 400 mortgage is 600 of net worth. The assets rise
    # 10%, to 1100, so what the family owns went 600 -> 700: +16.7%, not +10%.
    # Reporting the gross-asset move understates every leveraged day.
    series = [
        _point("2026-01-01", 600, 600, 600),
        _point("2026-01-02", 700, 700, 600),
    ]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 116.666667], rel=1e-5)


def test_negative_equity_carries_the_day_flat_instead_of_inverting_the_curve():
    from portfolio.services.comparison import _twr_index

    # Both sides net out debt, so an account underwater on the assets priced
    # that day yields a negative pair. Dividing would flip the level through
    # zero and every later day inherits the sign.
    series = [
        _point("2026-01-01", 100, 100, 100),
        _point("2026-01-02", -50, -50, -40),
        _point("2026-01-03", 110, 110, 100),
    ]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 100.0, 110.0])
    assert all(level > 0 for level in index)


def test_an_all_zero_base_is_an_answer_not_a_missing_field():
    from portfolio.services.comparison import _twr_index

    # Nothing was priced on two consecutive days, so every base is a real 0.
    # Read as "the field is absent" this fell back to chaining against the
    # previous day's total -- restoring the bug the pair replaced, on the one
    # book least able to survive it.
    series = [
        _point("2026-01-01", 1000, 0, 0),
        _point("2026-01-02", 2000, 0, 0),
    ]
    index = _twr_index(series)

    assert list(index) == pytest.approx([100.0, 100.0])


# ------------------------------------------------- the TSE index as a benchmark

@pytest.fixture
def benchmarked(compared, write_prices):
    """The comparison fixture, plus the live price that makes it valuable."""
    write_prices({"kama_stock": Decimal("2000")})
    return compared


def _index_history(days, start_value=1_000_000.0, step=1.0):
    """Write one TEDPIX close per day, ending today."""
    from marketdata.models import MarketIndexData

    today = timezone.now().date()
    for offset in range(days):
        day = today - datetime.timedelta(days=days - 1 - offset)
        MarketIndexData.objects.create(
            date=_jalali(day),
            time="00:00:00",
            state="",
            index_overall=start_value * (step ** offset),
        )


def _benchmarks(account, **params):
    client = APIClient()
    client.force_authenticate(user=account.user)
    query = "&".join(f"{k}={v}" for k, v in {"account": account.id, **params}.items())
    return client.get(f"/api/analytics/benchmarks/?{query}")


def test_the_tse_index_is_drawn_when_there_is_history_for_it(benchmarked):
    """The line BrsApi could not sell us at any price.

    This was hard-coded unavailable for a real reason -- the paid provider
    publishes the index as a live snapshot only, so MarketIndexData held about
    two weeks of rows and plotting it would have been inventing a comparison.
    TGJU carries the full daily series, so the premise is gone and the benchmark
    has to actually appear.
    """
    _index_history(90, step=1.01)

    response = _benchmarks(benchmarked, window=90)

    assert response.status_code == 200, response.data
    unavailable = {row["key"] for row in response.data["unavailable"]}
    assert "tse_index" not in unavailable, response.data["unavailable"]
    assert response.data["series"], "no rows to plot"
    assert "tse_index" in response.data["series"][0]


def test_a_rising_index_reads_as_growth_from_100(benchmarked):
    """Indexed to 100, so the number is relative growth, not an index level.

    A TEDPIX around 6.5 million plotted raw would flatten every other series on
    the chart into a horizontal line at the axis floor.
    """
    _index_history(90, start_value=1_000_000.0, step=1.01)

    rows = _benchmarks(benchmarked, window=90).data["series"]
    values = [row["tse_index"] for row in rows if row.get("tse_index") is not None]

    assert values, "index column present but entirely null"
    assert values[0] == pytest.approx(100.0, abs=1.0), values[0]
    assert values[-1] > values[0], "a compounding index must rise"


def test_without_index_history_it_is_reported_unavailable_with_a_reason(benchmarked):
    """No rows must read as "no benchmark", never as a flat zero-return line."""
    response = _benchmarks(benchmarked, window=90)

    assert response.status_code == 200, response.data
    reasons = {row["key"]: row["reason"] for row in response.data["unavailable"]}
    assert "tse_index" in reasons
    assert "history" in reasons["tse_index"]
    assert "tse_index" not in (response.data["series"][0] if response.data["series"] else {})

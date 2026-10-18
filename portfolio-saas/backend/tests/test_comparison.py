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


def _row(data, key):
    return next(row for row in data["rows"] if row["key"] == key)


def test_the_picker_separates_what_you_hold_from_what_you_could_hold(compared):
    response = _get(compared)

    assert response.status_code == 200, response.data
    assert [row["key"] for row in response.data["holdings"]] == ["kama_stock"]
    targets = {row["key"] for row in response.data["targets"]}
    assert "gold_18k_gram" in targets
    # Property is valued from the owner's own marks, so it can never be an
    # investment alternative.
    assert "house_asset" not in targets
    # The table opens on the dollar, gold and the coin, and only offers what
    # the picker could also offer.
    assert "gold_18k_gram" in response.data["default_targets"]
    assert set(response.data["default_targets"]) <= targets


def test_the_same_money_in_gold_is_worth_what_gold_did_with_it(compared):
    response = _get(compared, subject="kama_stock", targets="gold_18k_gram")

    assert response.status_code == 200, response.data
    gold = _row(response.data, "gold_18k_gram")
    actual, alternative = gold["series"]
    assert actual["key"] == "kama_stock"
    assert alternative["key"] == "gold_18k_gram"
    # 1,000 x 1,000 Rial then 1,000 x 1,500 Rial = 2,500,000 Rial = 250,000 Toman.
    assert response.data["subject"]["invested_tomans"] == pytest.approx(250000, rel=1e-6)
    assert gold["invested_tomans"] == pytest.approx(250000, rel=1e-6)
    # Gold ran away from the stock over this window, so the road not taken wins.
    assert gold["value_tomans"] > gold["actual_tomans"]
    assert gold["difference_tomans"] < 0
    assert gold["difference_pct"] < 0
    assert len(actual["points"]) == len(alternative["points"]) > 1
    assert response.data["as_of"]


def test_break_even_is_the_stock_price_that_matches_the_alternative(compared):
    """The number the page exists for: "KAMA has to reach X Rial to match gold".

    At that price the 2,000 shares are worth exactly what the gold is, so the
    two must multiply back to the gold row's value. Stated in Rial, because
    that is how every other screen quotes a TSE price; in Toman it would read
    as a tenth of the price on the quote screen.
    """
    data = _get(compared, subject="kama_stock", targets="gold_18k_gram").data
    gold = _row(data, "gold_18k_gram")
    shares = data["subject"]["units"]

    assert shares == pytest.approx(2000)
    assert gold["breakeven_price"]["unit"] == "IRR"
    assert gold["breakeven_price"]["value"] * shares / 10 == pytest.approx(
        gold["value_tomans"], rel=1e-3
    )
    # Today's own price, in the same unit, so the two sit side by side.
    assert data["subject"]["price"]["unit"] == "IRR"
    assert data["subject"]["price"]["value"] * shares / 10 == pytest.approx(
        data["subject"]["value_tomans"], rel=1e-3
    )
    # Gold won, so the stock must rise to catch it.
    assert gold["breakeven_price"]["value"] > data["subject"]["price"]["value"]
    # And the gold itself is reported as grams held, not only as a total.
    assert gold["units"] * 4000 == pytest.approx(gold["value_tomans"], rel=0.02)


def test_one_uncomparable_target_does_not_blank_the_others(compared):
    data = _get(
        compared, subject="kama_stock", targets="gold_18k_gram,house_asset"
    ).data

    assert _row(data, "house_asset")["reason"] == "real_estate_not_comparable"
    assert _row(data, "gold_18k_gram")["value_tomans"] > 0


def test_a_target_that_cannot_answer_is_a_row_not_a_failed_request(compared):
    """The page asks one target at a time and keeps the rows it has, so a
    refusal has to come back as that target's row. As a 400 it read as "this
    holding cannot be compared" and blanked the table."""
    response = _get(compared, subject="kama_stock", targets="house_asset")

    assert response.status_code == 200, response.data
    assert _row(response.data, "house_asset")["reason"] == "real_estate_not_comparable"
    assert response.data["subject"]["invested_tomans"] == pytest.approx(250000, rel=1e-6)


def test_today_is_priced_live_not_at_yesterdays_close(compared, write_prices):
    """The panel stops at the last daily close; "worth today" must not. Both
    ends of the row move to the price the rest of the app values holdings at
    today -- `get_latest_prices`, which takes a live tick during the session and
    the official close once the session is over."""
    from portfolio.services.valuation import get_latest_prices

    write_prices({"kama_stock": Decimal("2500"), "gold_18k_gram": Decimal("5000")})
    today = get_latest_prices()

    data = _get(compared, subject="kama_stock", targets="gold_18k_gram").data
    gold = _row(data, "gold_18k_gram")

    assert gold["priced_live"] is True
    assert gold["end_date"] == timezone.now().date().isoformat()
    # 2,000 shares at today's Rial price, on both the row and the tile.
    worth = 2000 * float(today["kama_stock"]) / 10
    assert gold["actual_tomans"] == pytest.approx(worth)
    assert data["subject"]["value_tomans"] == pytest.approx(worth)
    assert data["subject"]["price"]["unit"] == "IRR"
    assert gold["value_tomans"] == pytest.approx(
        gold["units"] * float(today["gold_18k_gram"])
    )
    for curve in gold["series"]:
        assert curve["points"][-1]["date"] == timezone.now().date().isoformat()


def test_the_picker_says_which_holdings_have_money_to_replay(compared, asset_catalog):
    from portfolio.services.ledger import create_ledger_entry

    create_ledger_entry(
        account=compared, kind=LedgerEntry.Kind.OPENING_POSITION,
        asset=asset_catalog["gold_18k_gram"], quantity=Decimal("3"),
        occurred_at=timezone.now() - datetime.timedelta(days=40),
    )
    holdings = {row["key"]: row for row in _get(compared).data["holdings"]}

    assert holdings["kama_stock"]["priced"] is True
    assert holdings["gold_18k_gram"]["priced"] is False


def test_without_targets_it_answers_against_the_defaults(compared):
    from portfolio.services.comparison import DEFAULT_TARGETS

    data = _get(compared, subject="kama_stock").data

    # Every default is a row -- answered, or refused with its own reason.
    assert [row["key"] for row in data["rows"]] == list(DEFAULT_TARGETS)
    assert all("value_tomans" in row or row.get("reason") for row in data["rows"])


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
        f"/api/comparison/?account={other.id}"
        "&subject=kama_stock&targets=gold_18k_gram"
    )

    assert response.status_code == 400
    assert response.data["reason"] == "no_recorded_cost"


def test_comparing_an_asset_only_with_itself_is_refused(compared):
    response = _get(compared, subject="kama_stock", targets="kama_stock")

    assert response.status_code == 400
    assert response.data["reason"] == "missing_target"


def test_a_short_window_still_prices_an_older_purchase_at_its_own_day(compared):
    """The window says how much to LOOK at, never how far back to replay. A
    30-day view of a 60-day-old purchase must still buy the alternative at the
    price on the day the money was actually spent -- buying it at the window's
    opening price instead would silently rewrite the cost.
    """
    full = _row(_get(
        compared, subject="kama_stock", targets="gold_18k_gram", days=0,
    ).data, "gold_18k_gram")
    short = _row(_get(
        compared, subject="kama_stock", targets="gold_18k_gram", days=30,
    ).data, "gold_18k_gram")

    assert len(short["series"][0]["points"]) < len(full["series"][0]["points"])
    # Same money, same replay -- only the visible slice differs, so today's
    # value of the alternative is identical either way.
    assert short["value_tomans"] == pytest.approx(full["value_tomans"])
    assert short["invested_tomans"] == full["invested_tomans"]


def test_a_sale_returns_money_so_it_reduces_what_was_put_in(compared, asset_catalog):
    from portfolio.services.ledger import create_ledger_entry

    create_ledger_entry(
        account=compared, kind=LedgerEntry.Kind.SELL,
        asset=asset_catalog["kama_stock"], quantity=Decimal("500"),
        unit_price_tomans=Decimal("2000"),
        occurred_at=timezone.now() - datetime.timedelta(days=5),
    )
    data = _get(compared, subject="kama_stock", targets="gold_18k_gram").data

    # 250,000 put in, then 500 x 2,000 Rial = 100,000 Toman taken back out.
    assert data["subject"]["invested_tomans"] == pytest.approx(150000, rel=1e-6)
    assert _row(data, "gold_18k_gram")["invested_tomans"] == pytest.approx(150000, rel=1e-6)
    assert data["subject"]["units"] == pytest.approx(1500)


def test_a_dollar_quoted_target_is_not_reported_as_toman(compared, asset_catalog):
    """`_load_price_panel` leaves USD_QUOTED_KEYS in dollars -- the conversion
    runs later, inside daily_returns_matrix. Every other consumer takes
    pct_change() next, where a constant factor cancels, so nothing noticed. This
    page prints the number, so it must read the converted panel.
    """
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

    response = _get(compared, subject="kama_stock", targets="bitcoin_usd")
    # An honest refusal is acceptable; silently pricing dollars as Toman is
    # not, which is what the assertion below is really guarding.
    if response.status_code != 200:
        assert response.data["reason"]
        return
    row = _row(response.data, "bitcoin_usd")
    if "reason" in row:
        return
    # At 1,000 dollars a coin and 100,000 Toman a dollar, 250,000 Toman buys
    # 0.0025 of one. Read as Toman it would buy 250 -- a factor of 100,000.
    assert row["value_tomans"] == pytest.approx(row["invested_tomans"], rel=0.01)


def test_a_target_whose_price_series_stopped_ends_the_window(compared, asset_catalog):
    """A halted or delisted target used to draw a flat line from its last close
    to today, and the summary reported that stale number as what you would have
    made. The row's window STOPS at the last day both series really traded,
    which reports no stale price as today's outcome while still answering.
    """
    from marketdata.models import GoldCurrencyHistory

    dead = asset_catalog["euro_cash"]
    dead.brs_symbol = "EUR"
    dead.save(update_fields=["brs_symbol"])
    # Priced daily for a month, then nothing for the last 12 days -- inside the
    # 21-calendar-day drawing fill, outside the 5-session staleness bound.
    start = timezone.now() - datetime.timedelta(days=42)
    for offset in range(31):
        day = (start + datetime.timedelta(days=offset)).date()
        GoldCurrencyHistory.objects.create(
            symbol="EUR", date=_jalali(day), unit="تومان",
            close_price=Decimal("50000"),
        )
    last_real = (start + datetime.timedelta(days=30)).date()

    response = _get(
        compared, subject="kama_stock", targets="euro_cash,gold_18k_gram"
    )

    assert response.status_code == 200, response.data
    euro = _row(response.data, "euro_cash")
    assert euro["end_date"] == last_real.isoformat()
    assert last_real < timezone.now().date()
    ended = [w for w in euro["warnings"] if w["reason"] == "series_ended"]
    assert ended, euro["warnings"]
    assert last_real.isoformat() in ended[0]["detail"]
    for curve in euro["series"]:
        assert curve["points"][-1]["date"] == last_real.isoformat()
    # The holding's own figures come from the row that reaches today.
    assert response.data["subject"]["end_date"] == _row(
        response.data, "gold_18k_gram"
    )["end_date"]


# ----------------------------------------------------------------------
# Partial overlap and the range buttons.


@pytest.fixture
def late_target(compared, asset_catalog):
    """A target the warehouse only started carrying three weeks ago."""
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


def test_a_target_with_a_shorter_history_starts_where_the_data_starts(late_target):
    """Compare from the first day both series exist, and say so, instead of
    refusing because one of them does not reach back far enough.
    """
    account, first_shared = late_target

    row = _row(_get(
        account, subject="kama_stock", targets="emami_coin", days=0,
    ).data, "emami_coin")

    assert row["start_date"] == first_shared.isoformat()
    assert row["carried_in_tomans"] > 0
    assert row["actual_tomans"] > 0
    assert row["value_tomans"] > 0
    note = [w for w in row["warnings"] if w["reason"] == "history_starts_late"]
    assert note, row["warnings"]
    assert first_shared.isoformat() in note[0]["detail"]


@pytest.mark.parametrize("days", [0, 365, 180, 90])
def test_every_range_answers_with_a_verdict_or_a_reason(late_target, days):
    """Never a silent empty panel, and never a window longer than the one asked for."""
    account, _ = late_target

    response = _get(
        account, subject="kama_stock", targets="gold_18k_gram,emami_coin", days=days
    )

    if response.status_code != 200:
        assert response.data.get("reason"), response.data
        return
    for row in response.data["rows"]:
        if "reason" in row:
            continue
        assert row["series"][0]["points"], row
        assert row["start_date"] and row["end_date"]
        if days:
            # A day of slack: the window is inclusive of both endpoints.
            assert row["window_days"] <= days + 1, row


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


def test_the_index_is_named_in_labels_or_the_client_never_draws_it(benchmarked):
    """The client renders exactly the keys `labels` names.

    A column present in `series` but absent from `labels` is computed on every
    request and silently never drawn -- the failure mode is a feature that
    looks shipped from the backend and does not exist for the user.
    """
    _index_history(90, step=1.01)

    labels = _benchmarks(benchmarked, window=90).data["labels"]

    assert "tse_index" in labels, labels
    assert labels["tse_index"]


def test_benchmarks_accepts_thirty_day_window(benchmarked):
    _index_history(30, step=1.01)

    response = _benchmarks(benchmarked, window=30)

    assert response.status_code == 200, response.data
    assert response.data["window"] == 30
    assert response.data["series"]


def test_benchmarks_all_maps_to_lifetime_days(benchmarked):
    _index_history(90, step=1.01)

    response = _benchmarks(benchmarked, window="all")

    assert response.status_code == 200, response.data
    assert response.data["requested_window"] == "all"
    assert response.data["requested_window_days"] >= 30
    assert response.data["data_window"]["observations"] >= 2


def test_a_recent_purchase_compares_on_its_daily_closes_not_todays_one_tick(
    compared, asset_catalog, write_prices
):
    """Prod, 2026-10-15: فولاد bought eight sessions ago answered "no usable
    price history" for every target. Under 30 closes the warehouse series is
    set aside for the live table, which only holds ticks since the asset was
    first held -- one, today -- and that one tick won."""
    start = timezone.now() - datetime.timedelta(days=10)
    LedgerEntry.objects.filter(account=compared).delete()
    from portfolio.services.ledger import create_ledger_entry

    create_ledger_entry(
        account=compared, kind=LedgerEntry.Kind.BUY,
        asset=asset_catalog["kama_stock"], quantity=Decimal("1000"),
        unit_price_tomans=Decimal("1500"), occurred_at=start,
    )
    write_prices({"kama_stock": Decimal("20000")})

    gold = _row(_get(compared, subject="kama_stock", targets="gold_18k_gram").data, "gold_18k_gram")

    assert "reason" not in gold, gold
    assert gold["value_tomans"] > 0

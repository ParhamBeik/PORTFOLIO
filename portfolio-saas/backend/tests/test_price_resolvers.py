"""The price-resolution paths must agree with each other.

Three places independently answer "what was this asset worth on date D":

    portfolio.services.valuation.value_as_of                  (TWR boundaries)
    portfolio.services.valuation.compute_dynamic_net_worth_series  (the chart)
    portfolio.services.returns.toman_price_panel              (risk / optimizer)

`CLAUDE.md` states the invariant they share -- bound forward-fill at five
trading sessions, on the market's own calendar, and exclude the asset beyond
it -- and it has broken twice in production, most visibly as a fake two-day
portfolio cliff on 2026-07-06/07. Until now nothing pinned the three against
each other; each had its own tests, so a divergence only showed up as two
screens disagreeing about the same holding.

`_latest_archive_closes` / `_archive_replacements` are deliberately NOT in this
set. They answer a different question -- "should this suspect live quote be
replaced" -- and carry their own market-state logic, so a stale close they
refuse to offer as a price is still allowed to veto a corrupt one. Comparing
them here would assert an equivalence that is not supposed to hold.

Units differ by design and are normalised before comparison: TSE quotes are
Rial and cross the `/10` boundary at the VALUE, never at the price
(`marketdata.currency.holding_value_to_toman`), while the returns panel converts
its TSE columns to Toman up front (`tse_close_to_toman`). Everything below is
compared as a Toman unit price so the three are on one scale.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from portfolio.models import Account, Holding

QTY = Decimal("100")

#: Long enough that the returns panel uses its warehouse column rather than
#: falling through to the live-`Price` loader (`MIN_DAILY_RETURNS = 30`), which
#: is a different source with different units -- see the final test.
DEEP_HISTORY_DAYS = 40


def _candles(symbol, dates, price="3000"):
    from marketdata.models import MarketCandle

    MarketCandle.objects.bulk_create(
        MarketCandle(
            symbol=symbol,
            timeframe=MarketCandle.ADJUSTED,
            date_time=date,
            close_price=Decimal(price),
        )
        for date in dates
    )


def _jalali(now, days_ago):
    from portfolio.services.returns import to_jalali_str

    return to_jalali_str(now - timedelta(days=days_ago))


def _verdicts(user, account, asset, now):
    """What each resolver decides about one asset, on one comparable scale.

    Every entry is either ``price=<toman unit price>`` or ``excluded``. The
    reason codes differ between paths by design (the panel reports
    `price_gap_exceeded` on a column it drops; the chart simply stops adding
    the asset to a day's total), so agreement is asserted on the DECISION and
    the NUMBER, which is what a user actually sees.
    """
    from portfolio.services.returns import toman_price_panel
    from portfolio.services.valuation import (
        compute_dynamic_net_worth_series,
        value_as_of,
    )

    # TSE unit prices are quoted in Rial; the /10 lives at the value boundary.
    rial = Decimal("10") if asset.tse_symbol else Decimal("1")
    out = {}

    payload = value_as_of(user, account=account, as_of=now)
    item = next((i for i in payload["items"] if i["key"] == asset.key), None)
    out["value_as_of"] = (
        f"price={Decimal(str(item['unit_price'])) / rial:.4f}" if item else "excluded"
    )

    series = compute_dynamic_net_worth_series(user, account, days=5)
    if not series:
        out["net_worth_series"] = "excluded"
    else:
        total = Decimal(series[-1]["total"])
        out["net_worth_series"] = (
            "excluded" if total == 0 else f"price={total / QTY:.4f}"
        )

    panel, _excluded, _warnings = toman_price_panel(
        history_days=90, held_keys=frozenset({asset.key}), gate=True
    )
    is_excluded = any(item.get("key") == asset.key for item in _excluded)
    column = (
        panel[asset.key].dropna()
        if (asset.key in panel.columns and not is_excluded)
        else None
    )
    out["returns_panel"] = (
        f"price={Decimal(str(float(column.iloc[-1]))):.4f}"
        if column is not None and not column.empty
        else "excluded"
    )
    return out


def _assert_agree(user, account, asset, now, expected):
    verdicts = _verdicts(user, account, asset, now)
    assert set(verdicts.values()) == {expected}, (
        f"price resolvers disagree: {verdicts} (expected all {expected!r})"
    )


@pytest.fixture
def one_stock(asset_catalog, write_prices, make_user):
    """A single TSE holding, with a live quote that differs from the warehouse.

    The live price is deliberately NOT equal to any warehouse close, so a path
    that silently falls back to it is visible in the number rather than hidden
    behind a coincidence.
    """
    asset = asset_catalog["kama_stock"]
    write_prices({"kama_stock": Decimal("3500"), "usd_cash": Decimal("60000")})
    user = make_user(email="resolvers@test.test")
    account = Account.objects.create(user=user, name="Resolvers")
    Holding.objects.create(account=account, asset=asset, quantity=QTY)
    return user, account, asset


@pytest.mark.django_db
def test_resolvers_agree_when_the_symbol_prints_every_session(one_stock):
    """The baseline. If this drifts, nothing below is meaningful."""
    user, account, asset = one_stock
    now = timezone.now()
    _candles("کاما", [_jalali(now, d) for d in range(0, DEEP_HISTORY_DAYS)])

    # 3000 Rial -> 300 Toman.
    _assert_agree(user, account, asset, now, "price=300.0000")


@pytest.mark.django_db
def test_resolvers_agree_across_a_market_closure(one_stock):
    """A closure is not staleness: no session elapsed, so the close still stands.

    This is the direction that produced the fake portfolio cliff -- a guard
    counting CALENDAR days drops a live holding over the Thu/Fri weekend.
    """
    user, account, asset = one_stock
    now = timezone.now()
    # Prints up to nine days ago, then the whole exchange is shut: no other
    # symbol prints either, so the market calendar records no elapsed session.
    _candles(
        "کاما",
        [_jalali(now, d) for d in range(9, 9 + DEEP_HISTORY_DAYS)],
    )

    _assert_agree(user, account, asset, now, "price=300.0000")


@pytest.mark.django_db
def test_resolvers_agree_that_a_quarantined_close_is_skipped(one_stock):
    """A rejected row is not a price, for any of the three.

    The net-worth chart used to be the exception: it read `MarketCandle`
    without consulting `RejectedRecord` at all, so a `series_spike` the nightly
    validator had already caught still landed on the chart at full size. With
    100 shares and a 99,999 rial spike that is a one-day jump to 999,990 Toman
    on a book otherwise worth 30,000.
    """
    from marketdata.models import RejectedRecord

    user, account, asset = one_stock
    now = timezone.now()
    _candles("کاما", [_jalali(now, d) for d in range(1, DEEP_HISTORY_DAYS)])
    _candles("کاما", [_jalali(now, 0)], price="99999")
    RejectedRecord.objects.create(
        endpoint="stock_candle_adjusted",
        symbol="کاما",
        date=_jalali(now, 0),
        reason="series_spike",
    )

    _assert_agree(user, account, asset, now, "price=300.0000")


@pytest.mark.django_db
def test_resolvers_agree_to_drop_a_symbol_past_the_forward_fill_bound(one_stock):
    """The market kept trading and this symbol did not: the price is dead.

    Carrying it is how a delisted holding keeps its last close forever. All
    three must refuse -- including the panel, whose refusal shows up as the
    column being dropped rather than as a zero.
    """
    user, account, asset = one_stock
    now = timezone.now()
    # Deep history that stops nine days ago...
    _candles(
        "کاما",
        [_jalali(now, d) for d in range(9, 9 + DEEP_HISTORY_DAYS)],
    )
    # ...while another symbol prints on every session since, so the market-wide
    # calendar shows sessions elapsing that کاما did not participate in.
    _candles(
        "فولاد",
        [_jalali(now, d) for d in range(0, 9)],
        price="1000",
    )

    _assert_agree(user, account, asset, now, "excluded")

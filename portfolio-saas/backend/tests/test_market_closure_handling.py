"""An exchange closure must not be mistaken for a warehouse gap.

Integration tests: the discriminator reads DailyStockHistory volume/trade totals
and the panel index together, so the defect only reproduces with real rows in
the database — a pure-pandas test cannot express "the provider padded these days".

Background: the TSE was shut for 83 days across 1404-1405. The provider still
emitted a row per symbol for every closed day, carrying the previous price with
zero volume and zero trades. `_trim_to_contiguous` read the resulting hole in
MarketCandle as an ingest outage and discarded everything before it, collapsing
a 2,333-session history to 55 and making all four lookback windows identical.
"""
import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from marketdata.candles import market_closure_days
from marketdata.models import DailyStockHistory
from portfolio.services.returns import _closure_explained, _mask_closure_returns

pytestmark = pytest.mark.django_db


def _history(date, *, symbol="کاما", volume, trades, price="2475"):
    return DailyStockHistory(
        symbol=symbol, date=date, time="12:30",
        tno=trades, tvol=volume, tval=volume * 10,
        py=Decimal(price), pl=Decimal(price), plc=Decimal("0"),
        plp=0.0, pc=Decimal(price),
    )


def test_zero_volume_days_are_reported_as_closures():
    DailyStockHistory.objects.bulk_create([
        _history("1404-12-06", volume=36_421_072, trades=992),
        _history("1404-12-09", volume=0, trades=0),
        _history("1404-12-11", volume=0, trades=0),
        _history("1405-02-29", volume=12_000_000, trades=430),
    ])

    closures = market_closure_days(start="1404-12-01", end="1405-03-01")

    assert closures == {"1404-12-09", "1404-12-11"}


def test_a_traded_day_is_never_called_a_closure():
    # One real trade on the day is enough to make it a session, so a genuine
    # ingest hole elsewhere still gets caught rather than excused.
    DailyStockHistory.objects.bulk_create([
        _history("1404-12-09", volume=1, trades=1),
    ])

    assert market_closure_days(start="1404-12-01", end="1404-12-30") == set()


def test_closure_explained_is_false_without_any_closure_rows():
    left = pd.Timestamp("2026-02-25", tz=dt.timezone.utc)
    right = pd.Timestamp("2026-05-19", tz=dt.timezone.utc)

    # No DailyStockHistory rows at all -> nothing proves a closure, so the gap
    # must be treated as an ingest hole (fail closed).
    assert _closure_explained(left, right) is False


def test_the_return_bridging_a_long_break_is_masked():
    """Reopening after months holds one enormous 'daily' return. Drop just it."""
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-02-24", tz=dt.timezone.utc),
            pd.Timestamp("2026-02-25", tz=dt.timezone.utc),
            pd.Timestamp("2026-05-19", tz=dt.timezone.utc),  # 83 days later
            pd.Timestamp("2026-05-20", tz=dt.timezone.utc),
        ]
    )
    returns = pd.DataFrame({"کاما": [0.01, 0.02, 0.90, 0.01]}, index=index)

    masked = _mask_closure_returns(returns.copy(), index)

    assert pd.isna(masked.loc[index[2], "کاما"])  # the 90% bridge is gone
    # Everything either side survives -- this is the whole point of not trimming.
    assert masked.loc[index[0], "کاما"] == 0.01
    assert masked.loc[index[1], "کاما"] == 0.02
    assert masked.loc[index[3], "کاما"] == 0.01

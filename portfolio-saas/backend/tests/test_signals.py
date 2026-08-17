"""Indicator arithmetic, the backtest's lookahead guard, and the integrity gate.

The arithmetic tests need no database: `portfolio.services.signals` is pure
pandas over a price series, which is why it was written that way.
"""
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from portfolio.services import signals as sig


def _rising(n=300, step=0.002):
    return pd.Series(100 * (1.0 + pd.Series(np.full(n, step))).cumprod())


def test_rsi_saturates_at_the_extremes():
    """A series with no down days is RSI 100; no up days is RSI 0.

    This is the check that catches the common implementation error of using a
    rolling mean instead of Wilder's EWM smoothing, which never quite reaches
    either bound.
    """
    up = pd.Series(np.arange(1, 200, dtype=float))
    down = pd.Series(np.arange(200, 1, -1, dtype=float))
    assert float(sig.rsi(up).iloc[-1]) == pytest.approx(100.0, abs=0.01)
    assert float(sig.rsi(down).iloc[-1]) < 1.0


def test_rsi_of_a_flat_series_is_neutral_not_nan():
    """No gains AND no losses is 0/0. It must land on 50, not NaN, or every
    downstream comparison silently becomes False."""
    flat = pd.Series(np.full(120, 42.0))
    value = float(sig.rsi(flat).iloc[-1])
    assert not np.isnan(value)
    assert value == pytest.approx(50.0, abs=1e-6)


def test_macd_histogram_is_zero_for_a_flat_series():
    flat = pd.Series(np.full(150, 10.0))
    assert abs(float(sig.macd(flat)["histogram"].iloc[-1])) < 1e-9


def test_macd_histogram_turns_positive_on_an_upturn():
    """Fast EMA leads slow EMA out of a trough, so the histogram flips sign."""
    series = pd.concat([
        pd.Series(np.linspace(100, 60, 120)),   # decline
        pd.Series(np.linspace(60, 130, 120)),   # recovery
    ], ignore_index=True)
    hist = sig.macd(series)["histogram"]
    assert float(hist.iloc[-1]) > 0
    assert float(hist.iloc[110]) < 0


def test_price_index_reproduces_compounded_returns():
    r = pd.Series([0.10, -0.05, 0.02])
    got = float(sig.price_index_from_returns(r).iloc[-1])
    assert got == pytest.approx(100 * 1.10 * 0.95 * 1.02, abs=1e-9)


def test_describe_refuses_thin_history():
    """20 sessions of RSI is noise wearing a verdict; it must return nothing."""
    assert sig.describe(_rising(20)) is None
    assert sig.describe(_rising(sig.MIN_OBSERVATIONS + 5)) is not None


def test_describe_reports_which_trend_window_it_used():
    """`above_trend` means different things at 50 vs 200 sessions, so the window
    travels with the flag rather than being assumed by the reader."""
    short = sig.describe(_rising(120))
    long = sig.describe(_rising(400))
    assert short["trend_window"] == 50
    assert long["trend_window"] == 200


def test_a_steady_uptrend_reads_bullish():
    reading = sig.describe(_rising(400))
    assert reading["above_trend"] is True
    assert reading["macd_histogram"] > 0
    assert reading["stance"] == "bullish"


def test_a_steady_downtrend_reads_bearish():
    # A constant PERCENTAGE decline (pd.Series(...).cumprod()) is a pathological
    # fixture for MACD: the absolute daily drop shrinks as price shrinks, so
    # momentum-of-momentum (the histogram) legitimately turns positive well
    # before any reversal -- a real, narrow MACD property, not a bug. A steady
    # or worsening ABSOLUTE decline is what "downtrend" means here and is what
    # actually exercises the bearish path.
    drops = 0.05 + 0.001 * np.arange(400)
    falling = pd.Series(100 - np.cumsum(drops))
    reading = sig.describe(falling)
    assert reading["above_trend"] is False
    assert reading["macd_histogram"] < 0
    assert reading["stance"] == "bearish"


def test_backtest_does_not_peek_at_the_current_bar():
    """The position is decided from yesterday's closes.

    On a series that only ever rises, a crossover strategy MUST underperform
    buy-and-hold: it is flat until the fast average crosses up. A backtest that
    matched or beat it would be reading the bar it trades on -- the single most
    common way a strategy looks profitable and is not.
    """
    result = sig.backtest_crossover(_rising(500))
    assert result is not None
    assert result["strategy_return"] <= result["buy_and_hold_return"] + 1e-9
    assert 0.0 <= result["share_of_days_invested"] < 1.0


def test_backtest_refuses_a_window_it_cannot_fill():
    assert sig.backtest_crossover(_rising(50), fast=50, slow=200) is None


def test_backtest_counts_trades_and_bounds_drawdown():
    choppy = pd.Series(
        100 + 20 * np.sin(np.linspace(0, 12 * np.pi, 600))
    )
    result = sig.backtest_crossover(choppy, fast=20, slow=60)
    assert result is not None
    assert result["trades"] > 0
    assert -1.0 <= result["max_drawdown"] <= 0.0


@pytest.mark.django_db
def test_nightly_task_records_the_integrity_verdict_on_every_row():
    """A stance drawn from a gap-ridden series must carry that fact.

    1,072 of 1,346 symbols currently fail the gate while the archive backfills,
    so this flag is what stops the UI presenting those as actionable.
    """
    from unittest.mock import patch

    from marketdata import tasks
    from marketdata.models import AssetSignalSnapshot, MarketInstrument, SymbolIntegrity

    MarketInstrument.objects.create(
        source="tsetmc", symbol="GOOD", category="stock", eligible=True
    )
    MarketInstrument.objects.create(
        source="tsetmc", symbol="GAPPY", category="stock", eligible=True
    )
    SymbolIntegrity.objects.create(symbol="GOOD", passes_gate=True)
    SymbolIntegrity.objects.create(symbol="GAPPY", passes_gate=False)

    dates = pd.date_range("2025-01-01", periods=300, freq="D", tz="UTC")
    returns = pd.DataFrame(
        {"GOOD": np.full(300, 0.002), "GAPPY": np.full(300, 0.002)}, index=dates
    )

    # The task imports daily_returns_matrix locally from portfolio.services.returns,
    # so patching it at the source is both necessary and sufficient.
    with patch("portfolio.services.returns.daily_returns_matrix",
               return_value=(returns, [])):
        tasks.nightly_asset_signals(window_days=365)

    rows = {r.symbol: r for r in AssetSignalSnapshot.objects.all()}
    assert set(rows) == {"GOOD", "GAPPY"}
    assert rows["GOOD"].passes_integrity is True
    assert rows["GAPPY"].passes_integrity is False
    # Both still get a stance; the gate labels it rather than hiding it.
    assert rows["GAPPY"].stance == "bullish"
    assert rows["GAPPY"].observations == 300


@pytest.mark.django_db
def test_nightly_task_is_idempotent_within_a_day():
    from unittest.mock import patch

    from marketdata import tasks
    from marketdata.models import AssetSignalSnapshot, MarketInstrument

    MarketInstrument.objects.create(
        source="tsetmc", symbol="ONE", category="stock", eligible=True
    )
    dates = pd.date_range("2025-01-01", periods=300, freq="D", tz="UTC")
    returns = pd.DataFrame({"ONE": np.full(300, 0.001)}, index=dates)

    with patch("portfolio.services.returns.daily_returns_matrix",
               return_value=(returns, [])):
        tasks.nightly_asset_signals(window_days=365)
        tasks.nightly_asset_signals(window_days=365)

    assert AssetSignalSnapshot.objects.count() == 1

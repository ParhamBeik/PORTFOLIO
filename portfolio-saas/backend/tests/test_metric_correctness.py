"""Unit tests for the diagnostics metric fixes (Sortino annualization, period-return
off-by-one, diversification ratio floor, CVaR tail-size guard).

These are pure-function tests over small in-memory pandas Series -- no DB, no
django_db mark -- so they exercise the math directly and run fast (unit tests,
not integration: the functions under test have no I/O, so pinning them at the
DataFrame/Series boundary is the cheapest way to catch a regression in the
math itself).
"""
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from portfolio.services import diagnostics as diag_mod
from portfolio.services.diagnostics import (
    _calmar,
    _historical_var_cvar,
    _period_returns,
    _sharpe,
    _sortino,
)


def _dates(n, start="2024-01-01"):
    return pd.date_range(start, periods=n, freq="D", tz="UTC")


# ---------- Sortino annualization -------------------------------------------


def test_sortino_is_annualized_not_daily():
    # Symmetric-ish series so sigma ~= sigma_downside; before the fix, sortino
    # was ~sqrt(252)=15.87x sharpe because the denominator stayed daily while
    # the numerator was annualized.
    rng = np.random.default_rng(0)
    values = rng.normal(loc=0.0005, scale=0.01, size=500)
    series = pd.Series(values, index=_dates(500))

    sharpe = _sharpe(series, risk_free_annual=0.0)
    sortino = _sortino(series, risk_free_annual=0.0)

    assert sharpe != 0
    ratio = sortino / sharpe
    # For a roughly symmetric series, sortino and sharpe should be the same
    # order of magnitude (downside deviation ~= full deviation / sqrt(2)-ish).
    assert 0.3 < ratio < 3.0, f"sortino/sharpe = {ratio} looks unannualized"
    assert abs(ratio - 15.87) > 5, "sortino is still ~sqrt(252)x inflated"


def test_sortino_known_downside_deviation():
    # Returns alternate 0 and -0.02 -> downside deviation (around MAR=0) is
    # easy to hand-compute: mean(downside**2) = mean(0, 0.0004 alternating)
    # = 0.0002, daily dd = sqrt(0.0002); annualized = that * sqrt(252).
    values = [0.0, -0.02] * 100
    series = pd.Series(values, index=_dates(200))
    sortino = _sortino(series, risk_free_annual=0.0)

    mean_excess = np.mean(values)
    downside = np.clip(values, a_min=None, a_max=0.0)
    dd_daily = np.sqrt(np.mean(np.array(downside) ** 2))
    expected = (mean_excess / dd_daily) * np.sqrt(252)

    assert sortino == pytest.approx(expected, rel=1e-9)


# ---------- Period returns off-by-one ---------------------------------------


def test_period_returns_does_not_drop_first_day():
    # 10 known daily returns of 1% each. The "7d" window must compound every
    # day the date filter actually selects -- not silently drop the earliest
    # one (the old bug used wealth[window[0]] as the denominator, which
    # already had that first day's return baked in).
    values = [0.01] * 10
    series = pd.Series(values, index=_dates(10))
    result = _period_returns(series)

    last_date = series.index[-1]
    n_in_window = int((series.index >= last_date - pd.Timedelta(days=7)).sum())
    expected_7d = 1.01 ** n_in_window - 1.0
    assert result["7d"] == pytest.approx(expected_7d, rel=1e-9)

    # 30d/90d windows (fewer than 30/90 days of history) fall back to the
    # full 10-day compounding, again with all 10 days included.
    expected_full = 1.01 ** 10 - 1.0
    assert result["30d"] == pytest.approx(expected_full, rel=1e-9)


# ---------- Diversification ratio floor --------------------------------------


def test_diversification_ratio_not_floored():
    # Mathematically, weighted_avg_vol / portfolio_vol >= 1.0 for any
    # well-conditioned covariance (Cauchy-Schwarz on the correlation matrix)
    # -- a sub-1.0 result can only come from a broken/ill-conditioned
    # estimate. So this test targets the actual bug: the payload assembly
    # in portfolio_diagnostics() used to clamp any such value up to 1.0 via
    # max(_finite(div_ratio), 1.0), silently hiding the broken estimate.
    # Mock the covariance step to return a sub-1.0 ratio and assert it comes
    # through the payload unchanged.
    dates = _dates(20)
    returns = pd.DataFrame({"a": [0.001] * 20}, index=dates)
    weights = {"a": 1.0}

    with patch.object(diag_mod, "daily_returns_matrix", return_value=(returns, [])), \
         patch.object(diag_mod, "_diversification_ratio", return_value=0.7):
        result = diag_mod.portfolio_diagnostics(weights, Decimal("1000000"))

    assert result["metrics"]["diversification_ratio"] == pytest.approx(0.7)


# ---------- CVaR tail-size guard ----------------------------------------------


def test_cvar_none_when_tail_too_small():
    # 20 observations, alpha=0.95 -> 5% tail = ~1 point, well under the
    # 5-observation floor. CVaR must be None, not an average of 1-2 numbers.
    values = list(np.linspace(-0.05, 0.05, 20))
    series = pd.Series(values, index=_dates(20))
    var, cvar = _historical_var_cvar(series, alpha=0.95)
    assert cvar is None
    assert var is not None


def test_cvar_present_with_enough_tail_observations():
    values = list(np.linspace(-0.05, 0.05, 200))
    series = pd.Series(values, index=_dates(200))
    var, cvar = _historical_var_cvar(series, alpha=0.95)
    assert cvar is not None
    assert cvar <= var  # CVaR (tail average) is at least as bad as VaR


# ---------- Calmar: explicit None below the 36-month window ------------------


def test_calmar_none_when_window_too_short():
    # Only ~200 days of history, far short of the 36-month (1095-day) window.
    values = [0.001] * 200
    series = pd.Series(values, index=_dates(200))
    calmar, window_days = _calmar(series)
    assert calmar is None
    assert window_days < 1095

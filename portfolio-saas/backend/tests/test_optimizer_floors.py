"""Unit tests for the observations floor / graceful fallback / credibility ceiling.

Test type: unit. `optimize()`'s floor/fallback/credibility logic is pure
arithmetic over a returns DataFrame; `daily_returns_matrix` is monkeypatched
with a synthetic in-memory frame so each test is fast and needs no price
fixtures. `@pytest.mark.django_db` is still required because `optimize()`
unconditionally resolves asset classes via `resolve_universe()` (an ORM
query) even when the returns frame is faked -- that's genuine model access,
not something worth mocking away.
"""
from decimal import Decimal
from unittest import mock

import numpy as np
import pandas as pd
import pytest
from django.core.cache import cache

from portfolio.services.optimization import (
    EXPECTED_RETURN_CREDIBILITY_CEILING,
    MIN_OBSERVATIONS_PER_ASSET,
    SHARPE_CREDIBILITY_CEILING,
    UniverseTooSmall,
    optimize,
)

pytestmark = pytest.mark.django_db


def _dates(n, start="2020-01-01"):
    return pd.date_range(start, periods=n, freq="D", tz="UTC")


def _run(returns_df, **kwargs):
    """Patch daily_returns_matrix with a synthetic frame and call optimize()."""
    cache.clear()
    with mock.patch(
        "portfolio.services.optimization.daily_returns_matrix",
        return_value=(returns_df, []),
    ):
        return optimize(
            scenario=kwargs.pop("scenario", "equal_weight"),
            current_weights={},
            total_value_tomans=Decimal("1"),
            user=None,
            universe=list(returns_df.columns),
            **kwargs,
        )


# ---------- 1. shared window far below the 10x floor triggers fallback -----


def test_shallow_shared_window_triggers_fallback():
    n_assets, n_rows = 15, 400
    cols = [f"asset_{i}" for i in range(n_assets)]
    rng = np.random.default_rng(1)
    data = rng.normal(0.0003, 0.01, size=(n_rows, n_assets))
    df = pd.DataFrame(data, index=_dates(n_rows), columns=cols)
    # Every asset is valid every day except the last one, which is only
    # valid on 40 scattered days (>= MIN_DAILY_RETURNS=30, so it clears the
    # per-asset filter individually) -- the exact "45 rows for 200 assets"
    # pathology: it alone collapses the shared window.
    shallow = cols[-1]
    mask = np.zeros(n_rows, dtype=bool)
    mask[rng.choice(n_rows, size=40, replace=False)] = True
    df.loc[~mask, shallow] = np.nan

    result = _run(df)

    assert result["fallback_applied"] is True
    assert result["fallback_from_n_assets"] == n_assets
    dropped = {e["key"]: e for e in result["excluded_assets"] if e["reason"] == "insufficient_shared_history"}
    assert shallow in dropped
    assert dropped[shallow]["observations"] == 40
    assert result["n_assets"] == n_assets - 1
    assert result["observations"] >= result["required_observations"]
    assert result["required_observations"] == MIN_OBSERVATIONS_PER_ASSET * result["n_assets"]


# ---------- 2. well-conditioned frame does not fall back, is plausible -----


def test_well_conditioned_frame_no_fallback_and_plausible():
    n_assets, n_rows = 5, 300
    cols = [f"asset_{i}" for i in range(n_assets)]
    rng = np.random.default_rng(2)
    # ~7.5%/yr mean, ~24%/yr vol -- unremarkable, Sharpe well under the ceiling.
    data = rng.normal(0.0003, 0.015, size=(n_rows, n_assets))
    df = pd.DataFrame(data, index=_dates(n_rows), columns=cols)

    result = _run(df)

    assert result["fallback_applied"] is False
    assert "fallback_from_n_assets" not in result
    assert result["observations"] == n_rows
    assert result["credibility"]["plausible"] is True
    assert result["credibility"]["reasons"] == []


# ---------- 3. absurd Sharpe fails the credibility ceiling ------------------


def _absurd_drift_frame():
    """Strong daily drift, tiny noise -> triple-digit annualized SAMPLE return."""
    n_assets, n_rows = 4, 200
    cols = [f"asset_{i}" for i in range(n_assets)]
    rng = np.random.default_rng(3)
    data = rng.normal(0.02, 0.0005, size=(n_rows, n_assets))
    return pd.DataFrame(data, index=_dates(n_rows), columns=cols)


def test_absurd_sharpe_fails_credibility_ceiling():
    """The ceiling must still fire for an estimator that can produce nonsense.

    Pinned against `sample_mean` explicitly: that is the estimator whose output
    the ceiling exists to catch. It is no longer the default (see
    services/expected_returns.py), so leaving this on the default would test the
    ceiling against a number that can no longer reach it.
    """
    result = _run(_absurd_drift_frame(), expected_return_method="sample_mean")

    assert result["target_metrics"]["sharpe"] > SHARPE_CREDIBILITY_CEILING
    assert result["target_metrics"]["expected_return_annual"] > EXPECTED_RETURN_CREDIBILITY_CEILING
    assert result["credibility"]["plausible"] is False
    assert len(result["credibility"]["reasons"]) >= 1
    assert result["credibility"]["thresholds"] == {
        "sharpe": SHARPE_CREDIBILITY_CEILING,
        "expected_return_annual": EXPECTED_RETURN_CREDIBILITY_CEILING,
    }


def test_default_estimator_does_not_produce_the_absurd_sharpe():
    """The improvement, stated as a test.

    The same panel that drives `sample_mean` to a triple-digit expected return
    must not do so under the default estimator. A ceiling that keeps firing is a
    warning; an estimator that stops generating the nonsense is a fix.
    """
    frame = _absurd_drift_frame()
    naive = _run(frame, expected_return_method="sample_mean")
    default = _run(frame)

    assert naive["credibility"]["plausible"] is False
    assert default["credibility"]["plausible"] is True
    assert default["target_metrics"]["sharpe"] < SHARPE_CREDIBILITY_CEILING
    assert default["expected_return_method"] == "black_litterman"


# ---------- 4. fewer than 3 usable assets still raises UniverseTooSmall ----


def test_fewer_than_three_assets_raises():
    n_rows = 60
    cols = ["asset_0", "asset_1"]
    rng = np.random.default_rng(4)
    data = rng.normal(0.0003, 0.01, size=(n_rows, len(cols)))
    df = pd.DataFrame(data, index=_dates(n_rows), columns=cols)

    with pytest.raises(UniverseTooSmall):
        _run(df)

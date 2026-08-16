"""Per-asset and per-class risk breakdown on portfolio_diagnostics."""
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from portfolio.services import diagnostics as diag_mod
from portfolio.services.diagnostics import portfolio_diagnostics


def _dates(n, start="2024-01-01"):
    return pd.date_range(start, periods=n, freq="D", tz="UTC")


def _valuation(items):
    return {"items": items}


def test_by_asset_includes_excluded_and_ready_rows():
    dates = _dates(120)
    returns = pd.DataFrame(
        {
            "alpha": np.full(120, 0.001),
            "beta": np.full(120, 0.002),
        },
        index=dates,
    )
    excluded = [{"key": "gamma", "reason": "price_gap_exceeded"}]
    valuation = _valuation([
        {"key": "alpha", "asset": "Alpha", "class": "Stock", "value": 600, "is_house": False, "is_manual": False},
        {"key": "beta", "asset": "Beta", "class": "Gold", "value": 400, "is_house": False, "is_manual": False},
        {"key": "gamma", "asset": "Gamma", "class": "Gold", "value": 100, "is_house": False, "is_manual": False},
        {"key": "house", "asset": "House", "class": "Real Estate", "value": 1000, "is_house": True, "is_manual": False},
    ])
    weights = {"alpha": 0.6, "beta": 0.4}

    with patch.object(diag_mod, "daily_returns_matrix", return_value=(returns, excluded)), \
         patch.object(diag_mod, "_load_index_returns", return_value=None):
        payload = diag_mod.portfolio_diagnostics(
            weights,
            Decimal("1100"),
            valuation=valuation,
        )

    by_key = {row["key"]: row for row in payload["by_asset"]}
    assert set(by_key) == {"alpha", "beta", "gamma", "house"}
    assert by_key["alpha"]["status"] == "ready"
    assert by_key["alpha"]["metrics"] is not None
    assert by_key["gamma"]["status"] == "excluded"
    assert by_key["gamma"]["metrics"] is None
    assert by_key["house"]["status"] == "not_applicable"


def test_by_asset_class_partial_when_some_assets_excluded():
    dates = _dates(120)
    returns = pd.DataFrame({"only": np.full(120, 0.001)}, index=dates)
    valuation = _valuation([
        {"key": "only", "asset": "Only", "class": "Stock", "value": 700, "is_house": False, "is_manual": False},
        {"key": "bad", "asset": "Bad", "class": "Stock", "value": 300, "is_house": False, "is_manual": False},
    ])
    excluded = [{"key": "bad", "reason": "insufficient_history"}]

    with patch.object(diag_mod, "daily_returns_matrix", return_value=(returns, excluded)), \
         patch.object(diag_mod, "_load_index_returns", return_value=None):
        payload = diag_mod.portfolio_diagnostics(
            {"only": 1.0},
            Decimal("1000"),
            valuation=valuation,
        )

    stock = next(row for row in payload["by_asset_class"] if row["asset_class"] == "Stock")
    assert stock["status"] == "partial"
    assert stock["held_count"] == 2
    assert stock["analyzable_count"] == 1
    assert stock["metrics"] is not None


def test_coverage_health_degraded_when_exclusions_exist():
    dates = _dates(120)
    returns = pd.DataFrame({"alpha": np.full(120, 0.001)}, index=dates)
    valuation = _valuation([
        {"key": "alpha", "asset": "Alpha", "class": "Stock", "value": 900, "is_house": False, "is_manual": False},
        {"key": "bad", "asset": "Bad", "class": "Stock", "value": 100, "is_house": False, "is_manual": False},
    ])
    excluded = [{"key": "bad", "reason": "integrity_gate_failed"}]

    with patch.object(diag_mod, "daily_returns_matrix", return_value=(returns, excluded)), \
         patch.object(diag_mod, "_load_index_returns", return_value=None):
        payload = diag_mod.portfolio_diagnostics(
            {"alpha": 1.0},
            Decimal("1000"),
            valuation=valuation,
        )

    assert payload["coverage"]["health"] == "degraded"
    assert payload["coverage"]["excluded_by_reason"]["integrity_gate_failed"] == 1
    assert payload["portfolio_full"]["concentration_hhi"] == pytest.approx(0.82, abs=0.01)


def test_portfolio_metrics_unchanged_for_single_asset():
    dates = _dates(120)
    returns = pd.DataFrame({"solo": np.full(120, 0.001)}, index=dates)
    weights = {"solo": 1.0}

    with patch.object(diag_mod, "daily_returns_matrix", return_value=(returns, [])), \
         patch.object(diag_mod, "_load_index_returns", return_value=None):
        payload = portfolio_diagnostics(weights, Decimal("1000"))

    assert payload["metrics"]["sharpe"] != 0
    assert payload["by_asset"] == []


def test_diversification_block_risk_contributions_sum_to_one():
    """Euler's theorem: risk contributions of a homogeneous-degree-1 vol
    function sum to exactly 1, so they read as percentages with no fudge.

    This is the number the household view is for -- "this holding is X% of my
    risk" -- and it was computed in services/diversification.py but never
    reached /api/analytics/ until it was wired into portfolio_diagnostics.
    """
    rng = np.random.default_rng(7)
    dates = _dates(200)
    # Deliberately unequal volatility and a correlated pair, so the risk split
    # cannot coincidentally equal the money split.
    quiet = rng.normal(0.0, 0.002, 200)
    loud = rng.normal(0.0, 0.030, 200)
    returns = pd.DataFrame(
        {"quiet": quiet, "twin": quiet * 0.98, "loud": loud}, index=dates
    )
    valuation = _valuation([
        {"key": "quiet", "asset": "Quiet", "class": "Cash", "value": 500,
         "is_house": False, "is_manual": False},
        {"key": "twin", "asset": "Twin", "class": "Cash", "value": 400,
         "is_house": False, "is_manual": False},
        {"key": "loud", "asset": "Loud", "class": "Crypto", "value": 100,
         "is_house": False, "is_manual": False},
    ])
    weights = {"quiet": 0.5, "twin": 0.4, "loud": 0.1}

    with patch.object(diag_mod, "daily_returns_matrix", return_value=(returns, [])), \
         patch.object(diag_mod, "_load_index_returns", return_value=None):
        payload = portfolio_diagnostics(weights, Decimal("1000"), valuation=valuation)

    block = payload["diversification"]
    contributions = block["risk_contributions"]
    assert set(contributions) == {"quiet", "twin", "loud"}
    assert sum(contributions.values()) == pytest.approx(1.0, abs=1e-6)

    # The 10% crypto sleeve is ~15x the volatility of the rest, so it must carry
    # far more risk than money. If these ever match, the decomposition is not
    # doing anything and the chart built on it would be decorative.
    assert contributions["loud"] > weights["loud"] * 2

    # Two of the three assets are 98% correlated, so they are ~one bet, not two.
    assert block["effective_bets"] < 3.0
    assert block["effective_holdings"] == pytest.approx(1 / sum(w**2 for w in weights.values()), abs=1e-3)

    gaps = {row["key"]: row for row in block["concentration_gap"]}
    assert gaps["loud"]["gap"] == pytest.approx(
        contributions["loud"] - weights["loud"], abs=1e-6
    )
    # Sorted worst-first so the UI can lead with the offender.
    assert block["concentration_gap"][0]["key"] == "loud"
    # A partially-covered book must not read as a fully-covered one.
    assert "mean_weight_covered" in block


def test_correlation_payload_is_square_and_unit_diagonal():
    """The heatmap's source. Reuses the panel diagnostics already built rather
    than triggering a second daily_returns_matrix pass."""
    rng = np.random.default_rng(11)
    dates = _dates(200)
    returns = pd.DataFrame(
        {
            "a": rng.normal(0.0, 0.01, 200),
            "b": rng.normal(0.0, 0.01, 200),
        },
        index=dates,
    )
    valuation = _valuation([
        {"key": "a", "asset": "A", "class": "Stock", "value": 500,
         "is_house": False, "is_manual": False},
        {"key": "b", "asset": "B", "class": "Stock", "value": 500,
         "is_house": False, "is_manual": False},
    ])

    with patch.object(diag_mod, "daily_returns_matrix", return_value=(returns, [])), \
         patch.object(diag_mod, "_load_index_returns", return_value=None):
        payload = portfolio_diagnostics(
            {"a": 0.5, "b": 0.5}, Decimal("1000"), valuation=valuation
        )

    corr = payload["correlation"]
    assets, matrix = corr["assets"], corr["matrix"]
    assert assets == ["a", "b"]
    assert len(matrix) == len(assets)
    for i, row in enumerate(matrix):
        assert len(row) == len(assets)
        assert row[i] == pytest.approx(1.0, abs=1e-9)
        for j, value in enumerate(row):
            assert matrix[j][i] == pytest.approx(value, abs=1e-9)


def test_diversification_degrades_honestly_on_a_single_asset():
    """One asset has no covariance structure. It must say so rather than
    reporting a confident 1.0 diversification ratio as if it were measured."""
    dates = _dates(120)
    returns = pd.DataFrame({"only": np.full(120, 0.001)}, index=dates)
    valuation = _valuation([
        {"key": "only", "asset": "Only", "class": "Gold", "value": 100,
         "is_house": False, "is_manual": False},
    ])

    with patch.object(diag_mod, "daily_returns_matrix", return_value=(returns, [])), \
         patch.object(diag_mod, "_load_index_returns", return_value=None):
        payload = portfolio_diagnostics({"only": 1.0}, Decimal("100"), valuation=valuation)

    block = payload["diversification"]
    assert block["risk_contributions"] == {}
    assert block["unavailable_reason"]

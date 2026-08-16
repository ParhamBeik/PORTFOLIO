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

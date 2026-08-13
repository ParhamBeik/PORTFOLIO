"""Diversification policy: correlation groups, balanced caps, policy version."""
import numpy as np
import pandas as pd
import pytest

from portfolio.services.classification import POLICY_VERSION, BALANCED_CONSTRAINTS, class_totals
from portfolio.services.optimization import (
    DiversificationInfeasible,
    _correlation_groups,
    _enforce_caps,
    _normalize_weights_or_raise,
)


pytestmark = pytest.mark.django_db


def test_correlation_groups_positive_threshold_only():
    idx = pd.date_range("2024-01-01", periods=40, freq="D")
    rng = np.random.default_rng(0)
    a = rng.normal(0, 0.01, size=40)
    b = a + rng.normal(0, 0.001, size=40)  # highly correlated with a
    c = -a + rng.normal(0, 0.001, size=40)  # negatively correlated — must not join
    d = rng.normal(0, 0.01, size=40)
    returns = pd.DataFrame({"a": a, "b": b, "c": c, "d": d}, index=idx)
    groups, mapping = _correlation_groups(returns, threshold=0.80)
    assert any(set(g) >= {"a", "b"} for g in groups)
    assert all("c" not in g or "a" not in g for g in groups)
    assert mapping.get("a") == mapping.get("b")


def test_enforce_caps_respects_class_and_group_limits():
    weights = {"s1": 0.5, "s2": 0.3, "g1": 0.2}
    class_map = {"s1": "Stock", "s2": "Stock", "g1": "Gold"}
    capped = _enforce_caps(
        weights,
        max_weight_per_asset=0.25,
        max_weight_per_class=BALANCED_CONSTRAINTS["max_weight_per_class"],
        class_map=class_map,
        max_weight_per_correlation_group=0.40,
        asset_to_group={"s1": 0, "s2": 0},
    )
    assert all(v <= 0.25 + 1e-6 for v in capped.values())
    assert sum(v for k, v in capped.items() if class_map[k] == "Stock") <= 0.60 + 1e-6
    assert sum(v for k, v in capped.items() if k in ("s1", "s2")) <= 0.40 + 1e-6


def test_normalize_raises_diversification_infeasible():
    with pytest.raises(DiversificationInfeasible) as exc:
        _normalize_weights_or_raise(
            {"a": 0.2, "b": 0.2},
            represented_classes=["Stock"],
            applicable_caps=BALANCED_CONSTRAINTS,
            correlation_groups=[],
        )
    assert "fully invested" in str(exc.value).lower() or "investable" in str(exc.value).lower()


def test_class_totals_and_policy_version_constant():
    totals = class_totals({"a": 0.6, "b": 0.4}, {"a": "Stock", "b": "Gold"})
    assert abs(sum(totals.values()) - 1.0) < 1e-9
    assert POLICY_VERSION == "balanced-v1"

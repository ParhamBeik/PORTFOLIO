"""Diversification policy: correlation clusters, balanced caps, policy version.

Unit tests: `_correlation_clusters` and `_enforce_caps` are pure functions over
in-memory frames/dicts, and `class_totals` is arithmetic — no DB, no network, so
they belong at the fast base of the pyramid rather than behind a database fixture.
"""
import numpy as np
import pandas as pd
import pytest

from portfolio.services.classification import (
    HARD_ASSET_SLEEVE,
    POLICY_VERSION,
    BALANCED_CONSTRAINTS,
    asset_class_map,
    class_totals,
)
from portfolio.services.optimization import (
    _correlation_clusters,
    _enforce_caps,
    summarize_optimizer_inputs,
)


def test_correlation_clusters_group_positively_correlated_assets_only():
    idx = pd.date_range("2024-01-01", periods=40, freq="D")
    rng = np.random.default_rng(0)
    a = rng.normal(0, 0.01, size=40)
    b = a + rng.normal(0, 0.001, size=40)  # highly correlated with a
    c = -a + rng.normal(0, 0.001, size=40)  # negatively correlated — must not join
    d = rng.normal(0, 0.01, size=40)
    returns = pd.DataFrame({"a": a, "b": b, "c": c, "d": d}, index=idx)

    clusters = _correlation_clusters(returns, threshold=0.80)

    # a and b land together; c never joins them despite |corr| being high.
    assert any({"a", "b"} <= set(cluster) for cluster in clusters)
    assert all(not {"a", "c"} <= set(cluster) for cluster in clusters)
    # Every column is accounted for exactly once.
    assert sorted(x for cluster in clusters for x in cluster) == ["a", "b", "c", "d"]


def test_enforce_caps_respects_asset_class_and_cluster_limits():
    weights = {"s1": 0.5, "s2": 0.3, "g1": 0.2}
    class_map = {"s1": "Stock", "s2": "Stock", "g1": "Gold"}

    capped = _enforce_caps(
        weights,
        max_weight_per_asset=0.25,
        max_weight_per_class=BALANCED_CONSTRAINTS["max_weight_per_class"],
        class_map=class_map,
        correlation_clusters=[["s1", "s2"]],
        max_weight_per_correlation_cluster=0.40,
    )

    assert all(v <= 0.25 + 1e-6 for v in capped.values())
    assert sum(v for k, v in capped.items() if class_map[k] == "Stock") <= 0.60 + 1e-6
    assert sum(v for k, v in capped.items() if k in ("s1", "s2")) <= 0.40 + 1e-6
    # Capping never invents weight.
    assert sum(capped.values()) <= 1.0 + 1e-6


def test_class_totals_and_policy_version_constant():
    totals = class_totals({"a": 0.6, "b": 0.4}, {"a": "Stock", "b": "Gold"})
    assert abs(sum(totals.values()) - 1.0) < 1e-9
    assert POLICY_VERSION == "balanced-v1"


def test_enforce_caps_hard_asset_sleeve():
    weights = {"usd_cash": 0.40, "gold_18k_gram": 0.40, "kama_stock": 0.20}
    class_map = {"usd_cash": "Cash", "gold_18k_gram": "Gold", "kama_stock": "Stock"}
    sleeves = [{
        "id": HARD_ASSET_SLEEVE["id"],
        "assets": ["usd_cash", "gold_18k_gram"],
        "max_combined_weight": HARD_ASSET_SLEEVE["max_weight"],
    }]

    capped = _enforce_caps(
        weights,
        max_weight_per_asset=0.40,
        max_weight_per_class={"Gold": 0.60, "Cash": 0.80, "Stock": 0.50},
        class_map=class_map,
        sleeves=sleeves,
    )

    hard = capped.get("usd_cash", 0.0) + capped.get("gold_18k_gram", 0.0)
    assert hard <= HARD_ASSET_SLEEVE["max_weight"] + 1e-6
    assert capped.get("kama_stock", 0.0) >= 0.20 - 1e-6
    assert sum(capped.values()) <= 1.0 + 1e-6


@pytest.mark.django_db
def test_catalog_gold_and_cash_classes(asset_catalog):
    cls = asset_class_map(["emami_coin", "gold_18k_gram", "usd_cash", "kama_stock"])
    assert cls["emami_coin"] == "Gold"
    assert cls["gold_18k_gram"] == "Gold"
    assert cls["usd_cash"] == "Cash"
    assert cls["kama_stock"] == "Stock"
    assert set(HARD_ASSET_SLEEVE["classes"]) == {"Gold", "Cash"}


def test_summarize_optimizer_inputs_flags_gold_cash_pairs():
    idx = pd.date_range("2024-01-01", periods=40, freq="D")
    rng = np.random.default_rng(4)
    gold = rng.normal(0.002, 0.01, size=40)
    usd = rng.normal(0.002, 0.01, size=40)
    stock = rng.normal(0.0003, 0.015, size=40)
    returns = pd.DataFrame(
        {"gold_18k_gram": gold, "usd_cash": usd, "kama_stock": stock}, index=idx
    )
    class_map = {"gold_18k_gram": "Gold", "usd_cash": "Cash", "kama_stock": "Stock"}

    summary = summarize_optimizer_inputs(returns, class_map, cluster_threshold=0.65)

    assert summary["observations"] == 40
    keys = {row["key"] for row in summary["assets"]}
    assert keys == {"gold_18k_gram", "usd_cash", "kama_stock"}
    pair_keys = {(p["a"], p["b"]) for p in summary["gold_cash_pairs"]}
    assert ("gold_18k_gram", "usd_cash") in pair_keys or ("usd_cash", "gold_18k_gram") in pair_keys
    assert all("would_cluster" in p for p in summary["gold_cash_pairs"])

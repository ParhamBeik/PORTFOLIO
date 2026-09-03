"""The optimizer and the risk numbers around it: frontier, weight floors, diversification policy, and metric correctness.

Merged from 7 files; each section keeps its original banner.
"""

from datetime import timedelta
import datetime
from decimal import Decimal
import os
from unittest import mock
from unittest.mock import patch

from django.core.cache import cache
from django.db.models import Case, DateTimeField, When
from django.utils import timezone
import jdatetime
import numpy as np
import pandas as pd
import pytest
from rest_framework.test import APIClient

from accounts.models import User
from marketdata.models import GoldCurrencyHistory, MarketCandle, MarketInstrument, SymbolIntegrity
from portfolio.models import Account, Asset, Holding, Price
from portfolio.models import Asset
from portfolio.optimization_models import OptimizationSnapshot
from portfolio.services import diagnostics as diag_mod
from portfolio.tasks import SCENARIOS, WINDOWS_DAYS, run_best_overall_snapshots
from portfolio.services.classification import (
    HARD_ASSET_SLEEVE,
    POLICY_VERSION,
    BALANCED_CONSTRAINTS,
    asset_class_map,
    class_totals,
)
from portfolio.services.diagnostics import (
    _calmar,
    _historical_var_cvar,
    _period_returns,
    _sharpe,
    _sortino,
)
from portfolio.services.diagnostics import _portfolio_returns, portfolio_diagnostics
from portfolio.services.diagnostics import portfolio_diagnostics
from portfolio.services.optimization import (
    EXPECTED_RETURN_CREDIBILITY_CEILING,
    MIN_OBSERVATIONS_PER_ASSET,
    SHARPE_CREDIBILITY_CEILING,
    UniverseTooSmall,
    optimize,
)
from portfolio.services.optimization import (
    UniverseTooSmall,
    _correlation_clusters,
    _efficient_frontier,
    _enforce_caps,
    _rebalance_trades,
    optimize,
)
from portfolio.services.optimization import (
    _correlation_clusters,
    _enforce_caps,
    summarize_optimizer_inputs,
)
from portfolio.services.returns import (
    DEFAULT_HISTORY_DAYS,
    RETURNS_CACHE_KEY,
    _price_version_fingerprint,
    daily_returns_matrix,
    invalidate_returns_cache,
)
from portfolio.services.returns import (
    _build_returns_matrix,
    _gap_profile,
    daily_returns_matrix,
    periods_per_year,
)

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_optimization.py
# Pro portfolio-optimization engine: returns pipeline, scenarios, caching, gating.
# 
# The synthetic_history fixture writes ~40 days of Price rows for 4 liquid assets
# with KNOWN daily-return profiles (low-vol emami_coin, alternating bitcoin_usd,
# flat usd_cash, drift kama_stock) plus usd_cash so the USD->Toman conversion
# path is exercised. Tests then assert the engine recovers the known values and
# that the scenario optimizers produce well-formed, constraint-respecting
# weights.
# 
# NB: `Price.fetched_at` is `auto_now_add`, so explicit `fetched_at` values on
# create are silently overridden. The fixtures use a CASE-expression bulk UPDATE
# after bulk_create to backfill the intended timestamps.


# ---------- fixture helpers -------------------------------------------------


def _backfill_fetched_at(rows: list[Price], timestamps: list) -> None:
    """After bulk_create, force each row's `fetched_at` to the timestamp at its index.

    `auto_now_add` overwrites the explicit `fetched_at` (both in memory and in
    the DB) during create, so we bulk_create then UPDATE with a CASE expression.
    The intended timestamps must be supplied separately because bulk_create
    clobbers `rows[i].fetched_at` in place.
    """
    if not rows:
        return
    when_cases = [When(id=r.pk, then=ts) for r, ts in zip(rows, timestamps)]
    Price.objects.update(fetched_at=Case(*when_cases, output_field=DateTimeField()))


def _seed_panel(asset_catalog, price_specs: dict, *, days: int = 42, kama_days: int | None = None) -> None:
    """Write a panel of daily prices for the given asset -> (initial_price, factor) specs.

    `factor` may be a single Decimal (same multiplier every day) or a callable
    `(day_index) -> Decimal`. `kama_days`, when set, writes only the most recent
    N days for `kama_stock` (used by test_min_history_exclusion).
    """
    now = timezone.now()
    rows = []
    timestamps = []
    for key, (initial, factor) in price_specs.items():
        p = Decimal(str(initial))
        for d in range(days):
            if key == "kama_stock" and kama_days is not None and d < days - kama_days:
                continue
            ts = now - timedelta(days=days - 1 - d)
            rows.append(Price(asset=asset_catalog[key], price=p, fetched_at=ts, source="TEST"))
            timestamps.append(ts)
            f = factor(d) if callable(factor) else factor
            p = (p * Decimal(str(f))).quantize(Decimal("0.0001"))
    Price.objects.bulk_create(rows)
    _backfill_fetched_at(rows, timestamps)
    cache.delete("prices:latest")
    invalidate_returns_cache()


# ---------- fixtures ---------------------------------------------------------


@pytest.fixture
def synthetic_history(asset_catalog, db):
    """~42 days of Price rows for 4 liquid assets + usd_cash.

    Daily-return profiles:
      * emami_coin: +0.1%/day (low vol)
      * bitcoin_usd: alternating +2%/-2% (high vol, USD-quoted)
      * usd_cash:   ~flat (small +/-0.01% noise) — the FX rail
      * kama_stock: +0.05%/day
    """
    _seed_panel(
        asset_catalog,
        {
            # emami: +0.1%/day (~28.5%/yr) — the known-value assertion checks this.
            "emami_coin": ("400000000.0", Decimal("1.001")),
            # bitcoin: net-positive alternation so one asset exceeds RFR(30%/yr)
            # for max_sharpe. +2.5%/-2% => ~0.5% every 2 days => ~65%/yr.
            "bitcoin_usd": ("60000.0", lambda d: Decimal("1.025") if d % 2 == 0 else (Decimal("1") / Decimal("1.02"))),
            "usd_cash": ("63000.0", lambda d: Decimal("1.0001") if d % 2 == 0 else Decimal("0.9999")),
            "kama_stock": ("5000.0", Decimal("1.0005")),
        },
    )
    return {k: asset_catalog[k] for k in
            ("emami_coin", "bitcoin_usd", "usd_cash", "kama_stock")}


@pytest.fixture
def kama_short_history(asset_catalog, db):
    """3 assets with 42 days + kama_stock with only 10 days (< MIN_DAILY_RETURNS)."""
    _seed_panel(
        asset_catalog,
        {
            "emami_coin": ("400000000.0", Decimal("1.001")),
            "bitcoin_usd": ("60000.0", lambda d: Decimal("1.025") if d % 2 == 0 else (Decimal("1") / Decimal("1.02"))),
            "usd_cash": ("63000.0", lambda d: Decimal("1.0001") if d % 2 == 0 else Decimal("0.9999")),
            "kama_stock": ("5000.0", Decimal("1.0005")),
        },
        kama_days=10,
    )
    return asset_catalog


@pytest.fixture
def correlated_gold_panel(asset_catalog, db):
    """3 correlated gold assets for HRP — they should cluster together.

    Uses small base prices to stay well under the Decimal(20,4) ceiling after
    42 days of noisy compounding.
    """
    rng = np.random.default_rng(42)
    # 5 assets x MIN_OBSERVATIONS_PER_ASSET(10) = 50 shared observations required
    # before the optimizer will solve. At 42 days this fixture tripped the
    # fallback instead, which dropped usd_cash -- and without its 0.80 Cash cap
    # the surviving gold cluster (0.50) plus crypto (0.30) cannot reach 1.0.
    # This test is about HRP clustering, so give it enough history to get there.
    days = 60
    # `base` is the per-day shared driver (a return, not a multiplier).
    base = rng.normal(0, 0.005, days)
    now = timezone.now()
    rows = []
    timestamps = []
    prices = {
        "emami_coin": Decimal("4000.0"),
        "half_coin": Decimal("2000.0"),
        "one_gram_coin": Decimal("900.0"),
        "usd_cash": Decimal("63000.0"),
        "bitcoin_usd": Decimal("60000.0"),
    }
    for d in range(days):
        ts = now - timedelta(days=days - 1 - d)
        for key in ("emami_coin", "half_coin", "one_gram_coin"):
            prices[key] = (prices[key] * Decimal(str(1.0 + base[d] + rng.normal(0, 0.001)))).quantize(Decimal("0.0001"))
        prices["usd_cash"] = (prices["usd_cash"] * Decimal(str(1.0 + rng.normal(0, 0.0002)))).quantize(Decimal("0.0001"))
        prices["bitcoin_usd"] = (prices["bitcoin_usd"] * Decimal(str(1.0 + rng.normal(0, 0.02)))).quantize(Decimal("0.0001"))
        for key, p in prices.items():
            rows.append(Price(asset=asset_catalog[key], price=p, fetched_at=ts, source="TEST"))
            timestamps.append(ts)
    Price.objects.bulk_create(rows)
    _backfill_fetched_at(rows, timestamps)
    cache.delete("prices:latest")
    invalidate_returns_cache()
    return asset_catalog


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _make_portfolio(user, catalog, holdings: dict, account_name="Main"):
    acct = Account.objects.create(user=user, name=account_name)
    for key, qty in holdings.items():
        Holding.objects.create(account=acct, asset=catalog[key], quantity=Decimal(str(qty)))
    return acct


# ---------- 1. daily returns known values -----------------------------------


def test_daily_returns_known_values(synthetic_history):
    df, excluded = daily_returns_matrix()
    assert "emami_coin" in df.columns
    assert "bitcoin_usd" in df.columns
    assert "usd_cash" in df.columns

    # emami: known +0.1%/day.
    emami_rets = df["emami_coin"].dropna()
    assert len(emami_rets) >= 30
    assert abs(emami_rets.mean() - 0.001) < 1e-3

    # bitcoin_usd is USD-quoted; the engine converts via usd_cash before
    # pct_change, so its TOMAN return = (1+usd_ret)(1+btc_usd_pure_ret) - 1.
    # Recover the raw USD bitcoin return by re-deriving it from the stored prices.
    from portfolio.models import Asset, Price
    btc_asset = Asset.objects.get(key="bitcoin_usd")
    usd_asset = Asset.objects.get(key="usd_cash")
    btc_raw = (
        pd.DataFrame.from_records(
            Price.objects.filter(asset=btc_asset).values("fetched_at", "price")
        )
        .assign(fetched_at=lambda d: pd.to_datetime(d["fetched_at"], utc=True))
        .set_index("fetched_at")
        .sort_index()
        .resample("1D")
        .last()
        ["price"]
        .astype(float)
        .pct_change()
    )
    usd_raw = (
        pd.DataFrame.from_records(
            Price.objects.filter(asset=usd_asset).values("fetched_at", "price")
        )
        .assign(fetched_at=lambda d: pd.to_datetime(d["fetched_at"], utc=True))
        .set_index("fetched_at")
        .sort_index()
        .resample("1D")
        .last()
        ["price"]
        .astype(float)
        .pct_change()
    )
    expected = ((1 + usd_raw) * (1 + btc_raw) - 1)
    btc_rets = df["bitcoin_usd"].dropna()
    aligned = pd.concat([btc_rets.rename("a"), expected.rename("b")], axis=1).dropna()
    assert np.allclose(aligned["a"], aligned["b"], atol=1e-6)


# ---------- 2. min history exclusion ----------------------------------------


def test_min_history_exclusion(kama_short_history):
    df, excluded = daily_returns_matrix()
    keys_excluded = {e["key"] for e in excluded}
    assert "kama_stock" in keys_excluded
    assert "kama_stock" not in df.columns
    assert "emami_coin" in df.columns


# ---------- 3. house excluded ------------------------------------------------


def test_house_excluded(synthetic_history):
    df, _ = daily_returns_matrix()
    assert "house_asset" not in df.columns


# ---------- 4. max_sharpe constraints ---------------------------------------


def test_optimization_max_sharpe_constraints(synthetic_history):
    user = User.objects.create_user(email="u1@t.t", password="Sup3rSecret!")
    _make_portfolio(
        user,
        synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    result = optimize(
        scenario="max_sharpe",
        current_weights={"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
        total_value_tomans=Decimal("1000000000"),
        user=user,
    )
    w = result["target_weights"]
    assert len(w) >= 1
    assert abs(sum(w.values()) - 1.0) < 1e-2
    cap_asset = result["constraints_applied"]["max_weight_per_asset"]
    for k, v in w.items():
        assert v <= cap_asset + 1e-6, f"{k}={v} exceeds cap {cap_asset}"
    class_cap = result["constraints_applied"]["max_weight_per_class"]
    by_class: dict[str, float] = {}
    for k, v in w.items():
        cls = Asset.objects.get(key=k).asset_class
        by_class[cls] = by_class.get(cls, 0.0) + v
    for cls, total in by_class.items():
        cap = class_cap.get(cls, 1.0)
        assert total <= cap + 1e-6, f"class {cls}={total} exceeds cap {cap}"


# ---------- 5. min_volatility ------------------------------------------------


def test_optimization_min_volatility(synthetic_history):
    current_weights = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3}
    user = User.objects.create_user(email="u2@t.t", password="Sup3rSecret!")
    result = optimize(
        scenario="min_volatility",
        current_weights=current_weights,
        total_value_tomans=Decimal("1000000000"),
        user=user,
    )
    w = result["target_weights"]
    assert abs(sum(w.values()) - 1.0) < 1e-2
    assert result["target_metrics"]["annualized_volatility"] >= 0
    df, _ = daily_returns_matrix()
    cols = [k for k in current_weights if k in df.columns]
    if cols:
        cw = np.array([current_weights[k] for k in cols], dtype=float)
        cw = cw / cw.sum()
        sub = df[cols].fillna(0.0).to_numpy()
        port = pd.Series(sub @ cw)
        cur_vol = float(port.std(ddof=1) * np.sqrt(252))
        assert result["target_metrics"]["annualized_volatility"] <= cur_vol + 1e-2


# ---------- 6. risk_parity (ERC) ---------------------------------------------


def test_optimization_risk_parity(synthetic_history):
    current_weights = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3}
    user = User.objects.create_user(email="u3@t.t", password="Sup3rSecret!")
    result = optimize(
        scenario="risk_parity",
        current_weights=current_weights,
        total_value_tomans=Decimal("1000000000"),
        user=user,
        # Equal risk contribution is exact only before concentration caps bind.
        # Cap enforcement is covered independently by the HRP/cap tests.
        constraints={
            "max_weight_per_asset": 1.0,
            "max_weight_per_class": {
                "Gold": 1.0,
                "Crypto": 1.0,
                "Stock": 1.0,
                "Cash": 1.0,
            },
        },
    )
    w = result["target_weights"]
    assert abs(sum(w.values()) - 1.0) < 1e-2
    for k, v in w.items():
        assert v >= -1e-6, f"{k} is negative: {v}"
    from sklearn.covariance import LedoitWolf
    df, _ = daily_returns_matrix()
    keys = [k for k in w if k in df.columns]
    if len(keys) >= 2:
        # Match the production estimator: incomplete observations are excluded,
        # never converted into synthetic zero returns.
        lw = LedoitWolf().fit(df[keys].dropna(how="any").to_numpy())
        cov = lw.covariance_ * 252
        wv = np.array([w[k] for k in keys])
        Sw = cov @ wv
        rc = wv * Sw
        rc_norm = rc / rc.sum() if rc.sum() else rc
        n = len(keys)
        for v in rc_norm:
            assert abs(v - 1.0 / n) < 0.20, f"rc {v} not near 1/{n}"


# ---------- 7. hrp with correlated gold -------------------------------------


def test_optimization_hrp(correlated_gold_panel):
    current_weights = {
        "emami_coin": 0.4,
        "half_coin": 0.3,
        "one_gram_coin": 0.3,
    }
    user = User.objects.create_user(email="u4@t.t", password="Sup3rSecret!")
    result = optimize(
        scenario="hrp",
        current_weights=current_weights,
        total_value_tomans=Decimal("1000000000"),
        user=user,
    )
    w = result["target_weights"]
    assert abs(sum(w.values()) - 1.0) < 1e-2
    for k, v in w.items():
        assert v >= -1e-6
        assert v <= 0.40 + 1e-6
    assert len(w) >= 2


# ---------- 8. efficient frontier shape -------------------------------------


def test_efficient_frontier_shape(synthetic_history):
    out = _efficient_frontier(n_points=30)
    frontier = out["frontier"]
    assert len(frontier) >= 20
    vols = [p["volatility"] for p in frontier]
    assert all(v > 0 for v in vols)
    assert vols == sorted(vols), "frontier must be vol-ascending"
    # Reference points present and well-formed.
    assert out["max_sharpe"] is not None
    assert out["min_volatility"] is not None
    assert "sharpe" in out["max_sharpe"]["metrics"]
    # The min_volatility point should sit at or near the lowest-vol frontier point.
    assert out["min_volatility"]["metrics"]["annualized_volatility"] <= vols[0] + 1e-2
    # Frontier must span a meaningful range (not collapsed to a single point).
    assert vols[-1] - vols[0] > 1e-4
    # Returns and Sharpes must be present and finite on every point.
    for p in frontier:
        assert np.isfinite(p["return"])
        assert np.isfinite(p["sharpe"])
    # At least one positive-Sharpe point exists (bitcoin drives the upside).
    assert any(p["sharpe"] > 0 for p in frontier)


# ---------- 9. rebalance trades sum to zero ---------------------------------


def test_rebalance_trades_sum_to_zero():
    current = {"emami_coin": 0.5, "bitcoin_usd": 0.3, "usd_cash": 0.2}
    target = {"emami_coin": 0.3, "bitcoin_usd": 0.4, "usd_cash": 0.3}
    trades = _rebalance_trades(current, target, Decimal("1000000000"))
    assert trades
    total_delta = sum(t["delta_weight_pct"] for t in trades)
    assert abs(total_delta) < 1e-6


# ---------- 10. diagnostics metrics finite ----------------------------------


def test_diagnostics_metrics_finite(synthetic_history):
    user = User.objects.create_user(email="u5@t.t", password="Sup3rSecret!")
    _make_portfolio(
        user,
        synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    weights = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3}
    diag = portfolio_diagnostics(weights, Decimal("1000000000"), user=user)
    metrics = diag["metrics"]
    for k, v in metrics.items():
        # None = the metric honestly refused to answer (e.g. calmar needs 36
        # months); strings are status markers like benchmark_status. Neither is
        # a number, but anything that IS a number must still be finite.
        if v is None or isinstance(v, str):
            continue
        assert np.isfinite(v), f"{k} not finite: {v}"
    # A refusal must say why it refused, not just go missing.
    if metrics.get("calmar") is None:
        assert "calmar_window_days" in metrics
    # >= 1.0 holds mathematically for any non-degenerate covariance. It is now a
    # real check: the artificial floor that used to mask a broken matrix is gone.
    assert metrics["diversification_ratio"] >= 1.0
    assert diag["eligible_assets"]
    assert isinstance(diag["total_value_tomans"], str)


# ---------- 11. returns cache invalidates on write --------------------------


def test_returns_cache_invalidates_on_write(synthetic_history, asset_catalog):
    from portfolio.services.returns import _returns_cache_key
    df1, _ = daily_returns_matrix()
    assert not df1.empty
    v1 = _price_version_fingerprint()
    assert cache.get(_returns_cache_key(DEFAULT_HISTORY_DAYS, None, None, "nominal", v1)) is not None

    Price.objects.create(
        asset=asset_catalog["emami_coin"],
        price=Decimal("500000000"),
        source="TEST",
    )
    invalidate_returns_cache()
    v2 = _price_version_fingerprint()
    assert v2 != v1
    # Old key gone after invalidate; new computation produces a fresh entry.
    df2, _ = daily_returns_matrix()
    assert cache.get(_returns_cache_key(DEFAULT_HISTORY_DAYS, None, None, "nominal", v2)) is not None


# ---------- 12. optimization cached -----------------------------------------


def test_optimization_cached(synthetic_history):
    user = User.objects.create_user(email="u6@t.t", password="Sup3rSecret!")
    current_weights = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3}
    payload = {
        "scenario": "max_sharpe",
        "current_weights": current_weights,
        "total_value_tomans": Decimal("1000000000"),
        "user": user,
    }
    r1 = optimize(**payload)
    assert r1["cached"] is False
    r2 = optimize(**payload)
    assert r2["cached"] is True


def test_optimization_cache_isolated_by_portfolio_state(synthetic_history):
    user = User.objects.create_user(email="u7@t.t", password="Sup3rSecret!")
    first = optimize(
        scenario="max_sharpe",
        current_weights={"emami_coin": 0.5, "bitcoin_usd": 0.3, "usd_cash": 0.2},
        total_value_tomans=Decimal("1000000000"),
        user=user,
    )
    second = optimize(
        scenario="max_sharpe",
        current_weights={"emami_coin": 0.2, "bitcoin_usd": 0.3, "usd_cash": 0.5},
        total_value_tomans=Decimal("1000000000"),
        user=user,
    )
    assert first["cached"] is False
    assert second["cached"] is False
    assert second["current_weights"]["usd_cash"] == 0.5


def test_correlation_cluster_cap_enforced(asset_catalog, db):
    """USD + gold moving together cannot exceed the cluster cap in target weights."""
    rng = np.random.default_rng(7)
    days = 42
    base = rng.normal(0.002, 0.003, days)
    now = timezone.now()
    rows = []
    timestamps = []
    prices = {
        "emami_coin": Decimal("4000.0"),
        "usd_cash": Decimal("63000.0"),
        "kama_stock": Decimal("5000.0"),
        "bitcoin_usd": Decimal("60000.0"),
    }
    for d in range(days):
        ts = now - timedelta(days=days - 1 - d)
        prices["emami_coin"] = (
            prices["emami_coin"] * Decimal(str(1.0 + base[d] + rng.normal(0, 0.0005)))
        ).quantize(Decimal("0.0001"))
        prices["usd_cash"] = (
            prices["usd_cash"] * Decimal(str(1.0 + base[d] + rng.normal(0, 0.0005)))
        ).quantize(Decimal("0.0001"))
        prices["kama_stock"] = (
            prices["kama_stock"] * Decimal(str(1.0 + rng.normal(0.001, 0.008)))
        ).quantize(Decimal("0.0001"))
        prices["bitcoin_usd"] = (
            prices["bitcoin_usd"]
            * (Decimal("1.025") if d % 2 == 0 else (Decimal("1") / Decimal("1.02")))
        ).quantize(Decimal("0.0001"))
        for key, p in prices.items():
            rows.append(Price(asset=asset_catalog[key], price=p, fetched_at=ts, source="TEST"))
            timestamps.append(ts)
    Price.objects.bulk_create(rows)
    _backfill_fetched_at(rows, timestamps)
    cache.delete("prices:latest")
    invalidate_returns_cache()

    universe = ["emami_coin", "usd_cash", "kama_stock", "bitcoin_usd"]
    df, _ = daily_returns_matrix(universe=universe)
    clusters = _correlation_clusters(df, 0.65)
    usd_gold = next(
        (c for c in clusters if "usd_cash" in c and "emami_coin" in c),
        None,
    )
    assert usd_gold is not None, "fixture should produce a USD+gold correlation cluster"

    user = User.objects.create_user(email="corr@t.t", password="Sup3rSecret!")
    result = optimize(
        scenario="min_volatility",
        current_weights={"emami_coin": 0.2, "usd_cash": 0.2, "kama_stock": 0.6},
        total_value_tomans=Decimal("1000000000"),
        user=user,
        universe=universe,
    )
    cap = result["constraints_applied"]["max_weight_per_correlation_cluster"]
    combined = sum(result["target_weights"].get(k, 0.0) for k in usd_gold)
    assert combined <= cap + 1e-6, f"cluster total {combined} exceeds cap {cap}"
    assert result["correlation_clusters"]


def test_hard_asset_sleeve_caps_uncorrelated_usd_and_gold(asset_catalog, db):
    """USD + 18k gold stay under the sleeve cap even when daily ρ < 0.65."""
    rng = np.random.default_rng(11)
    days = 80
    now = timezone.now()
    rows = []
    timestamps = []
    prices = {
        "gold_18k_gram": Decimal("3500.0"),
        "usd_cash": Decimal("63000.0"),
        "kama_stock": Decimal("5000.0"),
        "bitcoin_usd": Decimal("60000.0"),
    }
    gold_ret = rng.normal(0.003, 0.012, days)
    usd_ret = rng.normal(0.003, 0.012, days)
    stock_ret = rng.normal(0.0004, 0.016, days)
    btc_ret = rng.normal(0.0002, 0.03, days)
    for d in range(days):
        ts = now - timedelta(days=days - 1 - d)
        prices["gold_18k_gram"] = (
            prices["gold_18k_gram"] * Decimal(str(1.0 + float(gold_ret[d])))
        ).quantize(Decimal("0.0001"))
        prices["usd_cash"] = (
            prices["usd_cash"] * Decimal(str(1.0 + float(usd_ret[d])))
        ).quantize(Decimal("0.0001"))
        prices["kama_stock"] = (
            prices["kama_stock"] * Decimal(str(1.0 + float(stock_ret[d])))
        ).quantize(Decimal("0.0001"))
        prices["bitcoin_usd"] = (
            prices["bitcoin_usd"] * Decimal(str(1.0 + float(btc_ret[d])))
        ).quantize(Decimal("0.0001"))
        for key, p in prices.items():
            rows.append(Price(asset=asset_catalog[key], price=p, fetched_at=ts, source="TEST"))
            timestamps.append(ts)
    Price.objects.bulk_create(rows)
    _backfill_fetched_at(rows, timestamps)
    cache.delete("prices:latest")
    invalidate_returns_cache()

    universe = ["gold_18k_gram", "usd_cash", "kama_stock", "bitcoin_usd"]
    df, _ = daily_returns_matrix(universe=universe)
    corr = df[["gold_18k_gram", "usd_cash"]].corr().iloc[0, 1]
    assert corr < 0.65, f"fixture must stay below the cluster threshold, got {corr}"

    user = User.objects.create_user(email="sleeve@t.t", password="Sup3rSecret!")
    result = optimize(
        scenario="max_sharpe",
        current_weights={
            "gold_18k_gram": 0.2,
            "usd_cash": 0.2,
            "kama_stock": 0.5,
            "bitcoin_usd": 0.1,
        },
        total_value_tomans=Decimal("1000000000"),
        user=user,
        universe=universe,
    )
    w = result["target_weights"]
    hard = w.get("gold_18k_gram", 0.0) + w.get("usd_cash", 0.0)
    assert hard <= 0.50 + 1e-6, f"sleeve total {hard} exceeds 0.50"
    leftover = 1.0 - hard
    assert leftover >= 0.50 - 1e-6
    assert w.get("kama_stock", 0.0) > 0.0
    usd_gold_clustered = any(
        {"usd_cash", "gold_18k_gram"} <= set(c.get("assets") or [])
        for c in (result.get("correlation_clusters") or [])
    )
    assert not usd_gold_clustered
    assert result.get("sleeves")
    assert "hard_asset_sleeve_relaxed" not in (result.get("degraded") or [])


def test_infeasible_caps_are_rejected():
    constrained = _enforce_caps(
        {"gold_a": 0.5, "gold_b": 0.3, "gold_c": 0.2},
        max_weight_per_asset=0.4,
        max_weight_per_class={"Gold": 0.6},
        class_map={"gold_a": "Gold", "gold_b": "Gold", "gold_c": "Gold"},
    )
    assert sum(constrained.values()) == pytest.approx(0.6)
    assert max(constrained.values()) <= 0.4


# ---------- 13. analytics pro gated -----------------------------------------


def test_optimization_unknown_scenario_400(synthetic_history, make_user):
    pro = make_user(email="pro2@t.t")
    _make_portfolio(
        pro,
        synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    resp = _client(pro).post(
        "/api/optimization/", {"scenario": "nonsense"}, format="json"
    )
    assert resp.status_code == 400


# ---------- 16. universe too small 503 --------------------------------------


def test_optimization_universe_too_small_503(make_user, asset_catalog):
    pro = make_user(email="pro3@t.t")
    # Only 2 days of history for 2 assets -> excluded (< MIN_DAILY_RETURNS).
    now = timezone.now()
    rows = []
    timestamps = []
    for d in range(2):
        ts = now - timedelta(days=1 - d)
        rows.append(Price(asset=asset_catalog["emami_coin"], price=Decimal("400000000"), fetched_at=ts, source="TEST"))
        rows.append(Price(asset=asset_catalog["usd_cash"], price=Decimal("63000"), fetched_at=ts, source="TEST"))
        timestamps.append(ts)
        timestamps.append(ts)
    Price.objects.bulk_create(rows)
    _backfill_fetched_at(rows, timestamps)
    cache.delete("prices:latest")
    invalidate_returns_cache()
    _make_portfolio(pro, asset_catalog, {"emami_coin": 1.0})
    resp = _client(pro).post(
        "/api/optimization/", {"scenario": "max_sharpe"}, format="json"
    )
    assert resp.status_code == 503
    assert "eligible_assets" in resp.json()


# ---------- 17. frontier endpoint includes the random-weight cloud ----------


def test_my_optimal_returns_one_entry_per_window(synthetic_history, make_user):
    pro = make_user(email="my_optimal@t.t")
    acct = _make_portfolio(
        pro,
        synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    acct.tracking_started_at = timezone.now() - timedelta(days=40)
    acct.save(update_fields=["tracking_started_at"])

    resp = _client(pro).get(f"/api/optimization/my-optimal/?account={acct.id}")
    assert resp.status_code == 200
    body = resp.json()
    labels = [w["label"] for w in body["windows"]]
    assert labels == ["1Y", "3Y", "5Y", "Lifetime"]
    for window in body["windows"]:
        assert window["status"] in ("ok", "insufficient_history")
        if window["status"] == "ok":
            assert "max_sharpe" in window
            assert "actual" in window


def test_frontier_endpoint_includes_cloud(synthetic_history, make_user):
    pro = make_user(email="frontier_cloud@t.t")
    _make_portfolio(
        pro,
        synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    resp = _client(pro).get("/api/optimization/frontier/")
    assert resp.status_code == 200
    body = resp.json()
    assert "cloud" in body
    assert len(body["cloud"]) > 0
    for point in body["cloud"]:
        assert "return" in point and "volatility" in point
        assert point["volatility"] >= 0


# ---------- 18. held-book handling: freeze, proxy-merge, cap flooring --------
#
# These cover the "Optimal version of my portfolio" path, which differs from the
# market path (Best Overall) in that its universe is assets the user already
# owns. Screening those as optimizer *candidates* deleted them from the answer,
# and a deleted holding read as target=0, which the rebalance table rendered as
# "SELL ALL" -- a data-coverage verdict presented as investment advice.


def test_frozen_sleeve_keeps_unmeasurable_holdings_and_emits_no_trade():
    """Unit: a holding the solver could not measure keeps its weight, not a SELL."""
    from portfolio.services.optimization import _apply_frozen_sleeve

    current = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.2, "kama_stock": 0.1}
    solved = {"emami_coin": 0.45, "bitcoin_usd": 0.30, "usd_cash": 0.25}

    target, frozen, solved_share = _apply_frozen_sleeve(
        solved, current, set(solved)
    )

    assert frozen == {"kama_stock": 0.1}
    assert solved_share == pytest.approx(0.9)
    # Frozen weight is preserved exactly; the solved sleeve is scaled into what
    # is left, so the target still spans the whole portfolio.
    assert target["kama_stock"] == pytest.approx(0.1)
    assert target["emami_coin"] == pytest.approx(0.45 * 0.9)
    assert sum(target.values()) == pytest.approx(1.0)

    # The regression this exists to prevent: no trade for the frozen asset.
    trades = _rebalance_trades(current, target, Decimal("1000000"))
    assert all(t["key"] != "kama_stock" for t in trades)


def test_frozen_sleeve_is_noop_when_everything_is_measurable():
    from portfolio.services.optimization import _apply_frozen_sleeve

    current = {"a": 0.5, "b": 0.5}
    solved = {"a": 0.7, "b": 0.3}
    target, frozen, share = _apply_frozen_sleeve(solved, current, {"a", "b"})
    assert frozen == {}
    assert share == pytest.approx(1.0)
    assert target == pytest.approx(solved)


def test_proxy_group_collapse_and_expand_round_trip():
    """Unit: proxied holdings solve as ONE column and expand back pro-rata.

    Two Swiss bars priced off the same gold series are not two bets. Handing the
    solver duplicate columns makes the covariance singular and the split between
    them arbitrary.
    """
    from portfolio.services.optimization import (
        _collapse_onto_proxies,
        _expand_from_proxies,
    )

    groups = {"gold_18k_gram": ["swiss_gold_bar_1g", "swiss_gold_bar_2_5g"]}
    current = {"swiss_gold_bar_1g": 0.2, "swiss_gold_bar_2_5g": 0.6, "usd_cash": 0.2}

    collapsed = _collapse_onto_proxies(current, groups)
    assert collapsed == {"gold_18k_gram": pytest.approx(0.8), "usd_cash": pytest.approx(0.2)}

    expanded = _expand_from_proxies(
        {"gold_18k_gram": 0.5, "usd_cash": 0.5}, groups, current
    )
    # 0.5 split 1:3 by current weight (0.2 : 0.6).
    assert expanded["swiss_gold_bar_1g"] == pytest.approx(0.125)
    assert expanded["swiss_gold_bar_2_5g"] == pytest.approx(0.375)
    assert expanded["usd_cash"] == pytest.approx(0.5)
    assert sum(expanded.values()) == pytest.approx(1.0)


def test_proxy_expand_splits_evenly_when_group_has_no_current_weight():
    from portfolio.services.optimization import _expand_from_proxies

    groups = {"gold_18k_gram": ["swiss_gold_bar_1g", "swiss_gold_bar_2_5g"]}
    expanded = _expand_from_proxies({"gold_18k_gram": 0.4}, groups, {})
    assert expanded["swiss_gold_bar_1g"] == pytest.approx(0.2)
    assert expanded["swiss_gold_bar_2_5g"] == pytest.approx(0.2)


def test_caps_are_floored_to_what_the_book_already_holds():
    """Unit: a book breaching policy must still produce a target, not an error.

    A 68% Gold+Cash book breaches HARD_ASSET_SLEEVE (50%) on day one. Before
    this, that made full investment infeasible -> SolverError -> the page said
    "insufficient history", which is not what happened.
    """
    import copy as _copy
    from portfolio.services.optimization import (
        DEFAULT_CONSTRAINTS,
        _floor_constraints_for_book,
    )

    constraints = _copy.deepcopy(DEFAULT_CONSTRAINTS)
    current = {"emami_coin": 0.58, "usd_cash": 0.10, "bitcoin_usd": 0.32}
    class_map = {"emami_coin": "Gold", "usd_cash": "Cash", "bitcoin_usd": "Crypto"}

    floored = _floor_constraints_for_book(
        constraints, current, class_map, list(current), []
    )

    names = {f["cap"] for f in floored}
    assert "sleeve.hard_asset" in names          # 0.68 held vs 0.50 policy
    assert "max_weight_per_asset" in names       # 0.58 held vs 0.40 policy
    assert "max_weight_per_class.Crypto" in names  # 0.32 held vs 0.30 policy
    sleeve = next(f for f in floored if f["cap"] == "sleeve.hard_asset")
    assert sleeve["policy"] == pytest.approx(0.50)
    assert sleeve["floored_to"] == pytest.approx(0.68)
    # And the constraints dict itself was raised, so the solve is feasible.
    assert constraints["sleeves"][0]["max_weight"] == pytest.approx(0.68)
    assert constraints["max_weight_per_asset"] == pytest.approx(0.58)


def test_cap_flooring_leaves_a_compliant_book_untouched():
    import copy as _copy
    from portfolio.services.optimization import (
        DEFAULT_CONSTRAINTS,
        _floor_constraints_for_book,
    )

    constraints = _copy.deepcopy(DEFAULT_CONSTRAINTS)
    before = _copy.deepcopy(constraints)
    current = {"emami_coin": 0.2, "usd_cash": 0.2, "bitcoin_usd": 0.2, "kama_stock": 0.4}
    class_map = {
        "emami_coin": "Gold", "usd_cash": "Cash",
        "bitcoin_usd": "Crypto", "kama_stock": "Stock",
    }
    floored = _floor_constraints_for_book(
        constraints, current, class_map, list(current), []
    )
    assert floored == []
    assert constraints["max_weight_per_asset"] == before["max_weight_per_asset"]


def test_correlation_clusters_use_complete_linkage():
    """Unit: A~B and B~C must not merge A and C when A and C are uncorrelated.

    The previous union-find was transitive. On a rial-denominated book almost
    every pair clears the threshold through the shared devaluation factor, so
    that collapsed the whole universe into one cluster capped at 50% --
    infeasible at full investment by construction.
    """
    rng = np.random.default_rng(7)
    n = 300
    a = rng.normal(0, 1, n)
    c = rng.normal(0, 1, n)
    # b correlates ~0.9 with a and ~0.9 with c; a and c stay near zero.
    b = 0.7 * a + 0.7 * c
    df = pd.DataFrame({"a": a, "b": b, "c": c})

    corr = df.corr()
    assert corr.loc["a", "b"] >= 0.6
    assert corr.loc["b", "c"] >= 0.6
    assert abs(corr.loc["a", "c"]) < 0.3

    clusters = _correlation_clusters(df, 0.6)
    # No cluster may contain both a and c: their pairwise correlation is ~0.
    assert not any({"a", "c"} <= set(group) for group in clusters)


def test_my_optimal_freezes_short_history_holding_instead_of_selling_it(
    kama_short_history, make_user
):
    """Integration: the regression that motivated this work.

    kama_stock has 10 days of history (< MIN_DAILY_RETURNS), so the optimizer
    cannot measure it. It must keep its current weight and produce NO trade --
    not a SELL for the full position.
    """
    pro = make_user(email="frozen@t.t")
    acct = _make_portfolio(
        pro,
        kama_short_history,
        {"emami_coin": 1, "bitcoin_usd": 1, "usd_cash": 1000, "kama_stock": 100},
    )

    resp = _client(pro).get(f"/api/optimization/my-optimal/?account={acct.id}")
    assert resp.status_code == 200
    ok = [w for w in resp.json()["windows"] if w["status"] == "ok"]
    assert ok, "expected at least one solvable window"

    for window in ok:
        opt = window["max_sharpe"]
        # The target spans the whole book.
        assert sum(opt["target_weights"].values()) == pytest.approx(1.0, abs=1e-4)
        # Every held asset is accounted for -- in the target or explicitly frozen.
        for key in opt["current_weights"]:
            assert key in opt["target_weights"] or key in opt["frozen_weights"], key
        # A frozen holding keeps its weight and is never traded. `frozen_weights`
        # rounds to 6dp for display, `target_weights` does not, hence abs=1e-6.
        for key, info in opt["frozen_weights"].items():
            assert opt["target_weights"][key] == pytest.approx(info["weight"], abs=1e-6)
            assert opt["current_weights"][key] == pytest.approx(info["weight"], abs=1e-6)
            assert all(t["key"] != key for t in opt["rebalance_trades"])
            assert info["reason"]


def test_my_optimal_reports_asset_class_targets(synthetic_history, make_user):
    """Integration: the class roll-up the page is meant to answer."""
    pro = make_user(email="classes@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 1, "bitcoin_usd": 1, "usd_cash": 1000},
    )
    resp = _client(pro).get(f"/api/optimization/my-optimal/?account={acct.id}")
    assert resp.status_code == 200
    ok = [w for w in resp.json()["windows"] if w["status"] == "ok"]
    assert ok

    opt = ok[0]["max_sharpe"]
    current_c = opt["current_class_weights"]
    target_c = opt["target_class_weights"]
    assert current_c and target_c
    assert sum(current_c.values()) == pytest.approx(1.0, abs=1e-3)
    assert sum(target_c.values()) == pytest.approx(1.0, abs=1e-3)
    # Classes are named, not asset keys.
    assert set(target_c) <= {"Gold", "Cash", "Crypto", "Stock", "Real Estate", "Other"}
    # And the current column is comparable with the target on the same panel.
    assert "current_metrics" in opt
    assert "expected_return_annual" in opt["current_metrics"]


def test_frontier_is_scoped_to_held_assets(synthetic_history, make_user):
    """Integration: the frontier must cover the user's book, not the catalog.

    The chart caption promises "the assets you already hold"; without a universe
    the line and the Max-Sharpe marker described a portfolio the user cannot build.
    """
    pro = make_user(email="frontier_scope@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 1, "bitcoin_usd": 1, "usd_cash": 1000},
    )
    held = set(
        Holding.objects.filter(account=acct).values_list("asset__key", flat=True)
    )

    resp = _client(pro).get(f"/api/optimization/frontier/?account={acct.id}")
    assert resp.status_code == 200
    body = resp.json()
    if body.get("max_sharpe"):
        assert set(body["max_sharpe"]["weights"]) <= held


def test_optimize_annualizes_at_the_panels_measured_frequency(synthetic_history):
    """The panel's own sampling frequency, not a hardcoded 252.

    A gold/crypto book quotes 7 days a week; annualizing it at 252 understates
    volatility by sqrt(365/252) ~= 1.20x and changes which portfolio wins.
    """
    weights = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3}
    payload = optimize(
        scenario="min_volatility",
        current_weights=weights,
        total_value_tomans=Decimal("1000000"),
        held_keys=frozenset(weights),
    )
    frequency = payload["periods_per_year"]
    # This fixture writes one observation per calendar day.
    assert frequency == pytest.approx(365.25, rel=0.05)
    # The same measured frequency must reach the expected-return estimator, not
    # just the volatility scaling.
    assert payload["expected_return_provenance"]["periods_per_year"] == pytest.approx(
        frequency, rel=0.01
    )


def test_measured_asset_the_optimizer_exits_is_still_sold(synthetic_history, make_user):
    """A deliberate exit is not a freeze.

    Freezing must key off what REACHED the solver, not what came back holding
    weight. Keying off surviving weights would freeze every asset the optimizer
    measured and chose to exit, suppressing legitimate SELLs -- the mirror image
    of the bug the frozen sleeve exists to fix.
    """
    pro = make_user(email="exited@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 1, "bitcoin_usd": 1, "usd_cash": 1000, "kama_stock": 100},
    )
    resp = _client(pro).get(f"/api/optimization/my-optimal/?account={acct.id}")
    assert resp.status_code == 200
    ok = [w for w in resp.json()["windows"] if w["status"] == "ok"]
    assert ok

    for window in ok:
        opt = window["max_sharpe"]
        eligible = set(opt["eligible_assets"])
        frozen = set(opt["frozen_weights"])
        # Nothing that reached the solver may be reported as frozen.
        assert not (eligible & frozen), (eligible & frozen)
        # An eligible asset dropped to zero weight must produce a SELL.
        sold = {t["key"] for t in opt["rebalance_trades"] if t["action"] == "sell"}
        for key in eligible:
            if key in opt["current_weights"] and key not in opt["target_weights"]:
                assert key in sold, f"{key} was exited but no SELL was emitted"


# ---------- 19. Phase 2: diversification, robustness, estimators ------------
#
# The theme: diversification and downside are estimable from this much data;
# expected returns are not. These tests pin the parts that do not depend on
# forecasting, plus the honesty of the parts that do.


def _cov(frame):
    from portfolio.services.optimization import _shrunk_covariance
    return _shrunk_covariance(frame) * 365.0


def test_risk_contributions_sum_to_one_and_expose_the_weight_risk_gap():
    """A small position in a volatile asset can dominate portfolio risk."""
    from portfolio.services.diversification import (
        concentration_gap,
        risk_contributions,
    )

    rng = np.random.default_rng(3)
    n = 400
    frame = pd.DataFrame({
        "calm_a": rng.normal(0, 0.002, n),
        "calm_b": rng.normal(0, 0.002, n),
        "wild": rng.normal(0, 0.05, n),      # 25x the volatility
    })
    cov = _cov(frame)
    weights = {"calm_a": 0.45, "calm_b": 0.45, "wild": 0.10}

    contributions = risk_contributions(weights, cov)
    assert sum(contributions.values()) == pytest.approx(1.0, abs=1e-6)
    # 10% of the money, but the overwhelming majority of the risk.
    assert contributions["wild"] > 0.8

    gap = concentration_gap(weights, cov)
    assert gap[0]["key"] == "wild"
    assert gap[0]["risk_share"] > gap[0]["weight_share"]


def test_effective_bets_sees_through_correlated_duplicates():
    """Ten gold coins are one bet, not ten. Weight-based counting cannot tell."""
    from portfolio.services.diversification import effective_bets, effective_holdings

    rng = np.random.default_rng(11)
    n = 400
    driver = rng.normal(0, 0.01, n)
    # Five near-identical assets: same driver, trivial idiosyncratic noise.
    clones = pd.DataFrame({
        f"clone_{i}": driver + rng.normal(0, 0.0004, n) for i in range(5)
    })
    independent = pd.DataFrame({
        f"indep_{i}": rng.normal(0, 0.01, n) for i in range(5)
    })

    equal_clone = {c: 0.2 for c in clones.columns}
    equal_indep = {c: 0.2 for c in independent.columns}

    # Weight-based counting cannot tell these apart -- both look like 5 holdings.
    assert effective_holdings(equal_clone) == pytest.approx(5.0)
    assert effective_holdings(equal_indep) == pytest.approx(5.0)

    # Risk-based counting can: the clones collapse toward a single bet.
    clone_bets = effective_bets(equal_clone, _cov(clones))
    indep_bets = effective_bets(equal_indep, _cov(independent))
    assert clone_bets == pytest.approx(5.0, abs=0.5)  # RC are equal by symmetry...
    assert indep_bets == pytest.approx(5.0, abs=0.5)
    # ...so the DIVERSIFICATION RATIO is what separates them: correlated assets
    # cancel nothing, independent ones cancel a lot.
    from portfolio.services.diversification import diversification_ratio
    assert diversification_ratio(equal_clone, _cov(clones)) < 1.15
    assert diversification_ratio(equal_indep, _cov(independent)) > 1.8


def test_diversification_report_is_returned_for_current_and_target(
    synthetic_history, make_user
):
    pro = make_user(email="divers@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 1, "bitcoin_usd": 1, "usd_cash": 1000},
    )
    resp = _client(pro).get(f"/api/optimization/my-optimal/?account={acct.id}")
    assert resp.status_code == 200
    ok = [w for w in resp.json()["windows"] if w["status"] == "ok"]
    assert ok

    opt = ok[0]["min_volatility"]
    div = opt["diversification"]
    for side in ("current", "target"):
        report = div[side]
        assert sum(report["risk_contributions"].values()) == pytest.approx(1.0, abs=1e-4)
        assert report["effective_bets"] > 0
        assert report["diversification_ratio"] >= 1.0 - 1e-9
        assert "risk_by_class" in report
    # The target should not be LESS diversified than the current book -- that is
    # the entire point of running a min-variance optimizer.
    assert div["target"]["diversification_ratio"] >= div["current"]["diversification_ratio"] - 0.05


def test_shrinkage_pulls_expected_returns_toward_the_grand_mean():
    """Unit: the estimator must reduce cross-sectional spread, not preserve it."""
    from portfolio.services.expected_returns import sample_mean, shrunk

    rng = np.random.default_rng(5)
    n = 250
    # Same true mean, wildly different realized means -- exactly the situation
    # where the sample mean misleads and shrinkage helps.
    frame = pd.DataFrame({
        "lucky": rng.normal(0.004, 0.02, n),
        "unlucky": rng.normal(-0.003, 0.02, n),
        "flat": rng.normal(0.0, 0.02, n),
    })
    cov = _cov(frame)
    raw = sample_mean(frame, 365.0)
    pulled = shrunk(frame, 365.0, cov)

    assert pulled.std() < raw.std()
    assert 0.0 <= pulled.attrs["shrinkage_intensity"] <= 1.0
    # Order is preserved -- shrinkage moderates, it does not invert.
    assert pulled["lucky"] > pulled["unlucky"]


def test_black_litterman_equal_prior_is_risk_based_not_history_based():
    """BL with an equal-weight prior must rank by risk contribution, not by
    whichever asset happened to run up in the window."""
    from portfolio.services.expected_returns import black_litterman, sample_mean

    rng = np.random.default_rng(9)
    n = 300
    frame = pd.DataFrame({
        # Big realized return, small risk -- the sample mean loves this one.
        "lucky_calm": rng.normal(0.01, 0.002, n),
        # Negative realized return, large risk.
        "sad_wild": rng.normal(-0.002, 0.04, n),
    })
    cov = _cov(frame)
    raw = sample_mean(frame, 365.0)
    bl = black_litterman(frame, 365.0, cov)

    assert raw["lucky_calm"] > raw["sad_wild"]
    # BL reverse-optimizes from the covariance: the riskier asset must carry the
    # higher expected return, which is the opposite of the sample-mean ranking.
    assert bl["sad_wild"] > bl["lucky_calm"]


def test_estimate_mu_reports_the_standard_error_of_the_mean():
    """The error bar is the point: a mean you cannot measure must say so."""
    from portfolio.services.expected_returns import estimate_mu

    rng = np.random.default_rng(13)
    n = 365  # one year
    frame = pd.DataFrame({
        "a": rng.normal(0.0, 0.02, n),
        "b": rng.normal(0.0, 0.02, n),
        "c": rng.normal(0.0, 0.02, n),
    })
    cov = _cov(frame)
    _mu, provenance = estimate_mu(frame, 365.0, cov, method="shrunk")

    assert provenance["method"] == "shrunk"
    assert provenance["sample_years"] == pytest.approx(1.0, abs=0.05)
    # ~38% annualized vol over 1 year => SE of the mean is ~38%/yr. Enormous,
    # and larger than any return difference the optimizer would rank on.
    assert provenance["mean_standard_error"] > 0.2


def test_forecast_free_scenarios_are_flagged_as_such(synthetic_history):
    """min_volatility needs no mu; max_sharpe is entirely a bet on it."""
    weights = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3}
    common = dict(
        current_weights=weights,
        total_value_tomans=Decimal("1000000"),
        held_keys=frozenset(weights),
    )
    assert optimize(scenario="min_volatility", **common)["forecast_free"] is True
    assert optimize(scenario="max_sharpe", **common)["forecast_free"] is False


def test_resampling_reports_weight_bands(synthetic_history):
    """Robustness: the band is the deliverable, not the point estimate."""
    weights = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3}
    payload = optimize(
        scenario="min_volatility",
        current_weights=weights,
        total_value_tomans=Decimal("1000000"),
        held_keys=frozenset(weights),
        include_robustness=True,
    )
    rb = payload["robustness"]
    assert rb["converged"] > 0
    assert sum(rb["weights"].values()) == pytest.approx(1.0, abs=0.02)
    for key, band in rb["bands"].items():
        assert band["p05"] <= band["p95"]
        assert band["width"] == pytest.approx(band["p95"] - band["p05"], abs=1e-5)
    assert rb["max_band_width"] >= 0


def test_resampling_is_deterministic(synthetic_history):
    """A refresh must not move the allocation for no reason."""
    weights = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3}
    kwargs = dict(
        scenario="min_volatility",
        current_weights=weights,
        total_value_tomans=Decimal("1000000"),
        held_keys=frozenset(weights),
        include_robustness=True,
    )
    first = optimize(**kwargs)["robustness"]["weights"]
    cache.clear()
    second = optimize(**kwargs)["robustness"]["weights"]
    assert first == second


def test_min_cvar_scenario_solves_and_respects_caps(synthetic_history):
    """Downside-focused allocation: variance punishes upside too, CVaR does not."""
    weights = {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3}
    payload = optimize(
        scenario="min_cvar",
        current_weights=weights,
        total_value_tomans=Decimal("1000000"),
        held_keys=frozenset(weights),
    )
    target = payload["target_weights"]
    assert sum(target.values()) == pytest.approx(1.0, abs=1e-4)
    assert payload["forecast_free"] is True
    for key, w in target.items():
        assert w <= payload["constraints_applied"]["max_weight_per_asset"] + 1e-6


def test_robustness_endpoint_returns_bands(synthetic_history, make_user):
    pro = make_user(email="robust@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 1, "bitcoin_usd": 1, "usd_cash": 1000},
    )
    resp = _client(pro).get(
        f"/api/optimization/robustness/?account={acct.id}&scenario=min_volatility&window=365"
    )
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        body = resp.json()
        assert body["scenario"] == "min_volatility"
        assert body["forecast_free"] is True
        assert body["robustness"]["converged"] >= 0


# ----------------------------------------------------------------------
# test_optimizer_floors.py
# Unit tests for the observations floor / graceful fallback / credibility ceiling.
# 
# Test type: unit. `optimize()`'s floor/fallback/credibility logic is pure
# arithmetic over a returns DataFrame; `daily_returns_matrix` is monkeypatched
# with a synthetic in-memory frame so each test is fast and needs no price
# fixtures. `@pytest.mark.django_db` is still required because `optimize()`
# unconditionally resolves asset classes via `resolve_universe()` (an ORM
# query) even when the returns frame is faked -- that's genuine model access,
# not something worth mocking away.


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


# ----------------------------------------------------------------------
# test_best_overall.py
# Best Possible Portfolio Overall: nightly precompute + read-only view.


@pytest.fixture
def held_universe(synthetic_history):
    return ["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"]


def test_run_best_overall_snapshots_writes_one_per_window_and_scenario(held_universe):
    with mock.patch(
        "marketdata.universe.get_candidate_universe", return_value=(held_universe, [])
    ):
        result = run_best_overall_snapshots()

    assert result["ok"] is True
    snaps = OptimizationSnapshot.objects.filter(account=None)
    # synthetic_history is a short (~42 day) fixture, so only windows the
    # engine can actually solve over that history will have written rows --
    # the task must not raise on windows that can't solve, just skip them.
    assert snaps.count() > 0
    for snap in snaps:
        assert snap.window_days in WINDOWS_DAYS
        assert snap.scenario in SCENARIOS


def test_run_best_overall_snapshots_skips_when_universe_too_small():
    with mock.patch("marketdata.universe.get_candidate_universe", return_value=([], [])):
        result = run_best_overall_snapshots()
    assert result["ok"] is False
    assert OptimizationSnapshot.objects.count() == 0


def test_best_overall_view_reads_precomputed_snapshots(held_universe, make_user):
    with mock.patch(
        "marketdata.universe.get_candidate_universe", return_value=(held_universe, [])
    ):
        run_best_overall_snapshots()

    pro = make_user(email="best_overall@t.t")
    resp = _client(pro).get("/api/optimization/best-overall/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["windows"]) == len(WINDOWS_DAYS)
    labels = [w["label"] for w in body["windows"]]
    assert labels == ["1Y", "3Y", "5Y", "10Y"]
    assert any(w["status"] == "ok" for w in body["windows"])


def test_optimization_snapshot_views_keep_account_data_isolated(make_user):
    owner = make_user(email="snapshot-owner@test.test")
    other_user = make_user(email="snapshot-other@test.test")
    staff = make_user(email="snapshot-staff@test.test")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])

    owner_account = Account.objects.create(user=owner, name="Owner")
    other_account = Account.objects.create(user=other_user, name="Other")
    staff_account = Account.objects.create(user=staff, name="Staff")
    global_snapshot = OptimizationSnapshot.objects.create(
        account=None,
        payload={"scope": "global"},
    )
    owner_snapshot = OptimizationSnapshot.objects.create(
        account=owner_account,
        payload={"scope": "owner"},
    )
    other_snapshot = OptimizationSnapshot.objects.create(
        account=other_account,
        payload={"scope": "other"},
    )
    staff_snapshot = OptimizationSnapshot.objects.create(
        account=staff_account,
        payload={"scope": "staff"},
    )

    # The unscoped user-facing endpoints expose only market-wide snapshots;
    # staff status must not turn them into a cross-account data dump.
    staff_client = _client(staff)
    response = staff_client.get("/api/optimization/snapshots/")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [global_snapshot.id]

    response = staff_client.get("/api/optimization/snapshots/latest/")
    assert response.status_code == 200
    assert response.json()["id"] == global_snapshot.id

    # An account scope is valid only for an account owned by the caller.
    assert staff_client.get(
        f"/api/optimization/snapshots/?account_id={staff_account.id}"
    ).json()[0]["id"] == staff_snapshot.id
    assert staff_client.get(
        f"/api/optimization/snapshots/?account_id={owner_account.id}"
    ).status_code == 404
    assert staff_client.get(
        f"/api/optimization/snapshots/latest/?account_id={owner_account.id}"
    ).status_code == 404

    owner_client = _client(owner)
    assert owner_client.get(
        f"/api/optimization/snapshots/?account_id={owner_account.id}"
    ).json()[0]["id"] == owner_snapshot.id
    assert owner_client.get(
        f"/api/optimization/snapshots/?account_id={other_account.id}"
    ).status_code == 404
    assert owner_client.get(
        f"/api/optimization/snapshots/latest/?account_id={other_account.id}"
    ).status_code == 404


# ----------------------------------------------------------------------
# test_diversification_policy.py
# Diversification policy: correlation clusters, balanced caps, policy version.
# 
# Unit tests: `_correlation_clusters` and `_enforce_caps` are pure functions over
# in-memory frames/dicts, and `class_totals` is arithmetic — no DB, no network, so
# they belong at the fast base of the pyramid rather than behind a database fixture.


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


# ----------------------------------------------------------------------
# test_risk_breakdown.py
# Per-asset and per-class risk breakdown on portfolio_diagnostics.


def _dates_risk_breakdown(n, start="2024-01-01"):
    return pd.date_range(start, periods=n, freq="D", tz="UTC")


def _valuation(items):
    return {"items": items}


def test_by_asset_includes_excluded_and_ready_rows():
    dates = _dates_risk_breakdown(120)
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
    dates = _dates_risk_breakdown(120)
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
    dates = _dates_risk_breakdown(120)
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
    dates = _dates_risk_breakdown(120)
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
    dates = _dates_risk_breakdown(200)
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
    dates = _dates_risk_breakdown(200)
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
    dates = _dates_risk_breakdown(120)
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


def test_diversifier_ranking_prefers_uncorrelated_over_high_return():
    """Ranking must answer a diversification question, not a returns one.

    The trap this guards: sorting candidates by past return recommends whatever
    already went up, which on a gold-heavy book means more gold. A candidate that
    moves with what you already own must rank BELOW one that does not, even when
    its return is far better.
    """
    from portfolio.services.diversification import diversifier_candidates

    rng = np.random.default_rng(3)
    dates = _dates_risk_breakdown(300)
    portfolio = pd.Series(rng.normal(0.0, 0.02, 300), index=dates)

    candidates = pd.DataFrame({
        # Moves in lockstep with the book and is MORE volatile, and had a great
        # year. (A perfect twin of equal volatility would score exactly 0: at any
        # weight, (1-w)s + ws == s. Only extra volatility makes it actively worse.)
        "twin_high_return": portfolio.to_numpy() * 1.5 + 0.004,
        # Moves against the book, and had a mediocre year.
        "hedge_low_return": -portfolio.to_numpy() * 0.9 + 0.0001,
    }, index=dates)

    rows = diversifier_candidates(portfolio, candidates, entry_weight=0.05)
    ranked = [r["key"] for r in rows]
    assert ranked[0] == "hedge_low_return", (
        "a negatively-correlated candidate must outrank a perfectly-correlated "
        "one regardless of return"
    )

    by_key = {r["key"]: r for r in rows}
    assert by_key["twin_high_return"]["total_return"] > by_key["hedge_low_return"]["total_return"]
    # Adding more of what you already own increases volatility: the benefit is
    # negative, not merely small.
    assert by_key["twin_high_return"]["vol_reduction"] < 0
    assert by_key["hedge_low_return"]["vol_reduction"] > 0
    assert by_key["hedge_low_return"]["correlation"] < -0.9
    assert by_key["twin_high_return"]["correlation"] > 0.9


def test_diversifier_skips_candidates_without_enough_overlap():
    """A candidate sharing 10 days with the book cannot be scored honestly."""
    from portfolio.services.diversification import diversifier_candidates

    rng = np.random.default_rng(5)
    dates = _dates_risk_breakdown(300)
    portfolio = pd.Series(rng.normal(0.0, 0.02, 300), index=dates)
    short = pd.Series(rng.normal(0.0, 0.02, 300), index=dates)
    short.iloc[:290] = np.nan

    rows = diversifier_candidates(
        portfolio, pd.DataFrame({"barely_listed": short}), min_observations=60
    )
    assert rows == []


# ----------------------------------------------------------------------
# test_risk_data_gathering.py
# How the risk breakdown finds a held asset's history in the warehouse.
# 
# Integration, not unit: every defect these cover lived at the ORM -> panel ->
# matrix boundary (a SymbolIntegrity row, a leading run of NaNs, an Asset.proxy_key
# lookup), not in a pure function. The pre-existing suite mocked
# `daily_returns_matrix` wholesale, which is exactly why a 37%-weight holding could
# vanish from the risk card without a single test going red. These seed real rows
# and go through the real loader.


WINDOW_DAYS = 180


def _jalali_days(count, *, offset=0):
    """`count` consecutive Jalali date strings ending `offset` days before today."""
    today = jdatetime.date.today()
    return [
        (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(count + offset - 1, offset - 1, -1)
    ]


def _gold(symbol, days, *, start=1000.0, step=5.0):
    GoldCurrencyHistory.objects.bulk_create([
        GoldCurrencyHistory(
            symbol=symbol, date=day, close_price=start + i * step, unit="تومان"
        )
        for i, day in enumerate(days)
    ])


def _instrument(symbol, source=MarketInstrument.Source.BRS):
    return MarketInstrument.objects.create(
        symbol=symbol,
        name=symbol,
        source=source,
        category=MarketInstrument.Category.GOLD,
        eligible=True,
    )


def _asset(key, **kwargs):
    # Asset.save() runs full_clean, which requires an eligible MarketInstrument
    # for non-manual/non-house assets -- so seed the instrument first.
    kwargs.setdefault("name", key)
    kwargs.setdefault("is_active", True)
    return Asset.objects.create(key=key, **kwargs)


def _valuation_risk_data_gathering(items):
    return {"items": [
        {
            "key": key,
            "asset": key,
            "class": cls,
            "value": value,
            "is_house": cls == "Real Estate",
            "is_manual": False,
        }
        for key, cls, value in items
    ]}


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


# --- the gap profile: short history is not corruption -------------------------

def test_leading_nans_are_not_counted_as_a_price_gap():
    # 10 missing days at the front, then a clean run: the asset simply started
    # late. The old single-scan reported this as a 10-session gap.
    observed = np.array([False] * 10 + [True] * 30)
    assert _gap_profile(observed) == (10, 0)


def test_interior_and_trailing_holes_still_count():
    observed = np.array([False] * 3 + [True] * 5 + [False] * 7 + [True] * 5)
    assert _gap_profile(observed) == (3, 7)
    # A series that stops mid-window is a hole, not a short history.
    assert _gap_profile(np.array([True] * 5 + [False] * 9)) == (0, 9)


def test_short_history_survives_for_a_held_asset_but_not_a_screened_one():
    index = pd.date_range("2026-01-01", periods=60, tz="UTC")
    panel = pd.DataFrame(
        {
            "late": np.linspace(100.0, 160.0, len(index)),
            "complete": np.linspace(200.0, 260.0, len(index)),
        },
        index=index,
    )
    panel.loc[index[:40], "late"] = np.nan  # only 20 sessions of real history

    screened, excluded, _ = _build_returns_matrix(panel)
    assert "late" not in screened.columns
    assert {e["key"]: e["reason"] for e in excluded}["late"] == "insufficient_history"

    held, excluded_held, warnings = _build_returns_matrix(panel, frozenset({"late"}))
    assert "late" in held.columns, "a held asset keeps whatever history it has"
    assert not [e for e in excluded_held if e["key"] == "late"]
    assert {w["key"]: w["reason"] for w in warnings}["late"] == "short_history"


def test_a_real_interior_gap_excludes_even_a_held_asset():
    # Forward-filling past MAX_FORWARD_FILL_SESSIONS invents prices; a made-up
    # return is worse than a missing one, so this bar does not bend for holdings.
    index = pd.date_range("2026-01-01", periods=60, tz="UTC")
    panel = pd.DataFrame({"holed": np.linspace(100.0, 160.0, len(index))}, index=index)
    panel.loc[index[20:32], "holed"] = np.nan

    _, excluded, _ = _build_returns_matrix(panel, frozenset({"holed"}))
    assert {e["key"]: e["reason"] for e in excluded}["holed"] == "price_gap_exceeded"


# --- annualization frequency --------------------------------------------------

def test_periods_per_year_reads_the_calendar_not_a_constant():
    daily = pd.date_range("2026-01-01", periods=200, freq="D", tz="UTC")
    assert periods_per_year(daily) == pytest.approx(365.25, abs=1.0)

    # Sat-Wed: five sessions a week, spacings of 1,1,1,1,3 -- whose MEDIAN is 1.
    sessions = daily[~daily.dayofweek.isin([3, 4])]
    assert periods_per_year(sessions) == pytest.approx(261, abs=8)

    # An exchange closure is not the cadence and must not drag the figure down.
    with_closure = sessions.delete(range(40, 100))
    assert periods_per_year(with_closure) == pytest.approx(
        periods_per_year(sessions), abs=8
    )


# --- portfolio aggregation ----------------------------------------------------

def test_portfolio_returns_renormalize_per_day_instead_of_dropping_assets():
    index = pd.date_range("2026-01-01", periods=40, tz="UTC")
    returns = pd.DataFrame(
        {"a": np.full(40, 0.01), "b": np.full(40, 0.02)}, index=index
    )
    returns.iloc[5, returns.columns.get_loc("b")] = np.nan

    series = _portfolio_returns(returns, {"a": 0.6, "b": 0.4})

    assert len(series.index) == 40, "no row lost because one asset missed a day"
    assert series.iloc[0] == pytest.approx(0.6 * 0.01 + 0.4 * 0.02)
    # On the thin day the weights renormalize onto 'a' alone.
    assert series.iloc[5] == pytest.approx(0.01)
    assert set(series.attrs["weights_used"]) == {"a", "b"}
    assert series.attrs["dropped_assets"] == []
    assert series.attrs["mean_weight_covered"] == pytest.approx(1 - 0.4 / 40, abs=1e-6)


def test_many_assets_on_a_short_window_are_not_truncated_to_three():
    # The old drop-loop required 10 shared observations per asset and stopped at
    # 3 survivors, so a 10-asset book on a 60-session window lost half itself.
    index = pd.date_range("2026-01-01", periods=60, tz="UTC")
    keys = [f"a{i}" for i in range(10)]
    returns = pd.DataFrame({k: np.full(60, 0.01) for k in keys}, index=index)

    series = _portfolio_returns(returns, {k: 0.1 for k in keys})

    assert len(series.attrs["weights_used"]) == 10
    assert series.attrs["weights_rescaled"] is False
    assert series.iloc[0] == pytest.approx(0.01)


# --- the full path, through the ORM ------------------------------------------

@pytest.mark.django_db
def test_held_asset_with_a_failed_integrity_gate_is_reported_with_a_warning():
    days = _jalali_days(120)
    _instrument("IR_GOLD_18K")
    _instrument("IR_COIN_EMAMI")
    _gold("IR_GOLD_18K", days)
    _gold("IR_COIN_EMAMI", days, start=5000.0, step=11.0)
    _asset("gold_18k_gram", asset_class="Gold", brs_symbol="IR_GOLD_18K")
    _asset("emami_coin", asset_class="Gold", brs_symbol="IR_COIN_EMAMI")
    # The nightly screen fails it on a rejection ratio, despite full coverage.
    SymbolIntegrity.objects.create(
        symbol="IR_GOLD_18K", passes_gate=False, reason="excessive_rejections"
    )

    # Unheld, it is still screened out: the optimizer's universe is unchanged.
    screened, excluded = daily_returns_matrix(
        history_days=WINDOW_DAYS, universe=["gold_18k_gram", "emami_coin"]
    )
    assert "gold_18k_gram" not in screened.columns
    assert {e["key"]: e["reason"] for e in excluded}["gold_18k_gram"] == (
        "integrity_gate_failed"
    )

    valuation = _valuation_risk_data_gathering([("gold_18k_gram", "Gold", 600), ("emami_coin", "Gold", 400)])
    payload = portfolio_diagnostics(
        {"gold_18k_gram": 0.6, "emami_coin": 0.4},
        1000,
        history_days=WINDOW_DAYS,
        valuation=valuation,
    )

    row = {r["key"]: r for r in payload["by_asset"]}["gold_18k_gram"]
    assert row["status"] == "ready"
    assert row["metrics"] is not None
    assert row["metrics"]["annualized_volatility"] > 0
    assert "integrity_gate_failed" in row["warnings"]
    assert payload["coverage"]["analyzed_weight_pct"] == pytest.approx(1.0, abs=1e-6)
    assert payload["coverage"]["health"] == "degraded", "a caveat must be visible"


@pytest.mark.django_db
def test_manual_asset_borrows_its_proxy_series():
    days = _jalali_days(120)
    _instrument("IR_GOLD_18K")
    _gold("IR_GOLD_18K", days)
    _asset("gold_18k_gram", asset_class="Gold", brs_symbol="IR_GOLD_18K")
    _asset(
        "swiss_gold_bar_1g",
        asset_class="Gold",
        is_manual=True,
        proxy_key="gold_18k_gram",
    )

    valuation = _valuation_risk_data_gathering([
        ("gold_18k_gram", "Gold", 900),
        ("swiss_gold_bar_1g", "Gold", 100),
    ])
    payload = portfolio_diagnostics(
        {"gold_18k_gram": 0.9, "swiss_gold_bar_1g": 0.1},
        1000,
        history_days=WINDOW_DAYS,
        valuation=valuation,
    )

    rows = {r["key"]: r for r in payload["by_asset"]}
    bar, gold = rows["swiss_gold_bar_1g"], rows["gold_18k_gram"]
    assert bar["status"] == "ready"
    assert bar["proxied_from"] == "gold_18k_gram"
    assert "proxied" in bar["warnings"]
    assert bar["metrics"]["annualized_volatility"] == pytest.approx(
        gold["metrics"]["annualized_volatility"]
    )
    # Proxying is a modeling choice, not a data defect.
    assert payload["coverage"]["health"] == "healthy"


@pytest.mark.django_db
def test_proxies_stay_out_of_the_unheld_universe():
    # Two identical columns would give the optimizer a singular covariance and an
    # arbitrary choice between them, so proxy resolution is opt-in via held_keys.
    days = _jalali_days(120)
    _instrument("IR_GOLD_18K")
    _gold("IR_GOLD_18K", days)
    _asset("gold_18k_gram", asset_class="Gold", brs_symbol="IR_GOLD_18K")
    _asset(
        "swiss_gold_bar_1g",
        asset_class="Gold",
        is_manual=True,
        proxy_key="gold_18k_gram",
    )

    universe = ["gold_18k_gram", "swiss_gold_bar_1g"]
    screened, _ = daily_returns_matrix(history_days=WINDOW_DAYS, universe=universe)
    assert "swiss_gold_bar_1g" not in screened.columns

    held, _ = daily_returns_matrix(
        history_days=WINDOW_DAYS,
        universe=universe,
        held_keys=frozenset({"swiss_gold_bar_1g"}),
    )
    assert "swiss_gold_bar_1g" in held.columns


@pytest.mark.django_db
def test_real_estate_is_excluded_from_weights_but_stated_in_coverage():
    days = _jalali_days(120)
    _instrument("IR_GOLD_18K")
    _gold("IR_GOLD_18K", days)
    _asset("gold_18k_gram", asset_class="Gold", brs_symbol="IR_GOLD_18K")
    _asset("house_asset", asset_class="Real Estate", is_house=True)

    valuation = _valuation_risk_data_gathering([
        ("gold_18k_gram", "Gold", 700),
        ("house_asset", "Real Estate", 300),
    ])
    payload = portfolio_diagnostics(
        {"gold_18k_gram": 1.0}, 700, history_days=WINDOW_DAYS, valuation=valuation
    )

    rows = {r["key"]: r for r in payload["by_asset"]}
    assert rows["house_asset"]["status"] == "not_applicable"
    # The metrics describe 70% of the book, and the payload says so out loud.
    assert payload["coverage"]["analyzed_weight_pct"] == pytest.approx(0.7, abs=1e-6)


# ----------------------------------------------------------------------
# test_metric_correctness.py
# Unit tests for the diagnostics metric fixes (Sortino annualization, period-return
# off-by-one, diversification ratio floor, CVaR tail-size guard).
# 
# These are pure-function tests over small in-memory pandas Series -- no DB, no
# django_db mark -- so they exercise the math directly and run fast (unit tests,
# not integration: the functions under test have no I/O, so pinning them at the
# DataFrame/Series boundary is the cheapest way to catch a regression in the
# math itself).


# ---------- Sortino annualization -------------------------------------------


def test_sortino_is_annualized_not_daily():
    # Symmetric-ish series so sigma ~= sigma_downside; before the fix, sortino
    # was ~sqrt(252)=15.87x sharpe because the denominator stayed daily while
    # the numerator was annualized.
    rng = np.random.default_rng(0)
    values = rng.normal(loc=0.0005, scale=0.01, size=500)
    series = pd.Series(values, index=_dates_risk_breakdown(500))

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
    series = pd.Series(values, index=_dates_risk_breakdown(200))
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
    series = pd.Series(values, index=_dates_risk_breakdown(10))
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
    dates = _dates_risk_breakdown(20)
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
    series = pd.Series(values, index=_dates_risk_breakdown(20))
    var, cvar = _historical_var_cvar(series, alpha=0.95)
    assert cvar is None
    assert var is not None


def test_cvar_present_with_enough_tail_observations():
    values = list(np.linspace(-0.05, 0.05, 200))
    series = pd.Series(values, index=_dates_risk_breakdown(200))
    var, cvar = _historical_var_cvar(series, alpha=0.95)
    assert cvar is not None
    assert cvar <= var  # CVaR (tail average) is at least as bad as VaR


# ---------- Calmar: explicit None below the 36-month window ------------------


def test_calmar_none_when_window_too_short():
    # Only ~200 days of history, far short of the 36-month (1095-day) window.
    values = [0.001] * 200
    series = pd.Series(values, index=_dates_risk_breakdown(200))
    calmar, window_days = _calmar(series)
    assert calmar is None
    assert window_days < 1095


# ---------- BLAS kernel dispatch: the solver stack must not SIGILL -----------
#
# cvxpy pulls in scs, which bundles its own OpenBLAS (0.3.15) alongside numpy's
# and scipy's. On the deployed QEMU vCPU that copy auto-detects an AMD
# "Opteron" and dispatches dgemm_oncopy_OPTERON_SSE3, whose 9th byte is 0f 0e --
# FEMMS, a 3DNow! instruction the vCPU does not implement. Result: SIGILL on
# every matrix multiply that lands in it, ~46k killed pool workers/day
# (2026-08-26). scs's and scipy's both export an unsuffixed dgemm_, so whichever
# is dlopen'd first wins for the whole process; that is why the live worker
# crashed and the archive worker, same image, did not.
#
# OPENBLAS_CORETYPE (set in docker-compose.prod.yml) pins the kernel and skips
# the broken detection. This guards that: a base-image or wheel bump that
# reintroduces a bad auto-detect fails here instead of in production.


def _bundled_openblas_libs():
    import pathlib
    import sysconfig

    root = pathlib.Path(sysconfig.get_paths()["purelib"])
    return sorted(root.glob("*.libs/libopenblas*.so*"))


# Must run out-of-process: SIGILL is not catchable, it would take pytest with it.
_DGEMM_PROBE = r"""
import ctypes, os, sys
lib = ctypes.CDLL(sys.argv[1])
N = 128
d = ctypes.c_double
A = (d * (N * N))(*([1.0] * (N * N)))
B = (d * (N * N))(*([2.0] * (N * N)))
C = (d * (N * N))()
n = ctypes.c_int(N)
alpha, beta = d(1.0), d(0.0)
t = ctypes.c_char(b'N')
lib.dgemm_(ctypes.byref(t), ctypes.byref(t), ctypes.byref(n), ctypes.byref(n),
           ctypes.byref(n), ctypes.byref(alpha), A, ctypes.byref(n), B,
           ctypes.byref(n), ctypes.byref(beta), C, ctypes.byref(n))
assert C[0] == 256.0, C[0]
print("ok")
"""


@pytest.mark.parametrize("lib", _bundled_openblas_libs(), ids=lambda p: p.parent.name)
def test_bundled_openblas_dgemm_does_not_sigill(lib):
    """Every bundled OpenBLAS must survive a real dgemm under the deployed env."""
    import subprocess
    import sys

    env = dict(os.environ)
    # The libs sit beside their libgfortran/libquadmath dependencies.
    env["LD_LIBRARY_PATH"] = os.pathsep.join(
        filter(None, [str(lib.parent), env.get("LD_LIBRARY_PATH", "")])
    )
    proc = subprocess.run(
        [sys.executable, "-c", _DGEMM_PROBE, str(lib)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    # -4/132 is SIGILL: the Opteron kernel executing FEMMS.
    assert proc.returncode == 0, (
        f"{lib.name} exited {proc.returncode} (SIGILL is -4/132). "
        f"OPENBLAS_CORETYPE={env.get('OPENBLAS_CORETYPE', '<unset>')}. "
        f"stderr={proc.stderr[-500:]}"
    )


# ----------------------------------------------------------------------
# Holdings shared by two portfolios must be summed, not overwritten.
#
# `_liquid_items` flattens every account into one list, so an asset owned in two
# portfolios appears twice. Building the weight map with a dict comprehension
# keyed on the asset kept only the LAST row while `total` still counted both, so
# the weights quietly summed to less than 1 and every consumer -- the optimizer,
# the rebalance plan, the risk breakdown -- worked on a book smaller than the one
# the user actually holds.
#
# Unit tests: with the valuation injected there is no I/O left, so pinning the
# function boundary is the cheapest place to catch a regression in the math.


def _dup_valuation():
    """Two portfolios that both hold usd_cash and quarter_coin."""
    return {
        "total": Decimal("1000"),
        "accounts": [
            {"items": [
                {"key": "usd_cash", "class": "Cash", "value": Decimal("100")},
                {"key": "quarter_coin", "class": "Gold", "value": Decimal("400")},
                {"key": "kama_stock", "class": "Stock", "value": Decimal("300")},
            ]},
            {"items": [
                {"key": "usd_cash", "class": "Cash", "value": Decimal("150")},
                {"key": "quarter_coin", "class": "Gold", "value": Decimal("50")},
            ]},
        ],
    }


def test_weights_sum_to_one_when_an_asset_is_held_in_two_portfolios(monkeypatch):
    from portfolio import views as views_mod

    monkeypatch.setattr(views_mod, "value_user", lambda user: _dup_valuation())
    weights, total, _ = views_mod._current_weights_and_total(user=object())

    assert total == Decimal("1000")
    # The whole book is accounted for; nothing fell out of the map.
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-9)


def test_shared_holdings_are_summed_not_overwritten(monkeypatch):
    from portfolio import views as views_mod

    monkeypatch.setattr(views_mod, "value_user", lambda user: _dup_valuation())
    weights, _, _ = views_mod._current_weights_and_total(user=object())

    # 100 + 150, not the last row's 150 alone.
    assert weights["usd_cash"] == pytest.approx(0.25, abs=1e-9)
    # 400 + 50, not the last row's 50 alone -- the shape that hid 580,300,000 T.
    assert weights["quarter_coin"] == pytest.approx(0.45, abs=1e-9)
    assert weights["kama_stock"] == pytest.approx(0.30, abs=1e-9)


# ---------------------------------------------------------------------------
# cardinality -- "hold at most N assets"
# ---------------------------------------------------------------------------
#
# The constraint is a mixed-integer one, so `optimize()` relaxes it: solve, keep
# the N largest positions, re-solve on just those. These tests pin the two
# things that relaxation must still get right -- the count is respected, and the
# surviving weights come from a real second solve rather than a renormalization.


# Caps off, so the cardinality pass is the only thing shaping the answer. With
# the shipped policy caps in place a 3-asset gold/cash/crypto subset tops out at
# 0.80 -- which is its own test below, not the happy path.
UNCAPPED = {
    "max_weight_per_asset": 1.0,
    "max_weight_per_class": {},
    "max_weight_per_correlation_cluster": 1.0,
    "sleeves": [],
}


def _uncapped(**extra):
    return {**UNCAPPED, **extra}


@pytest.fixture
def cardinality_user(db):
    return User.objects.create_user(email="cardinality@t.t", password="Sup3rSecret!")


def _weights_arg():
    return {"emami_coin": 0.25, "bitcoin_usd": 0.25, "usd_cash": 0.25, "kama_stock": 0.25}


def test_max_assets_caps_the_position_count(synthetic_history, cardinality_user):
    """A 4-asset universe asked for 3 positions returns at most 3."""
    universe = ["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"]
    full = optimize(
        scenario="risk_parity", current_weights=_weights_arg(),
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=universe, constraints=_uncapped(),
    )
    assert len(full["target_weights"]) == 4, "fixture must start above the cap"

    limited = optimize(
        scenario="risk_parity", current_weights=_weights_arg(),
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=universe, constraints=_uncapped(max_assets=3),
    )
    assert len(limited["target_weights"]) <= 3
    assert sum(limited["target_weights"].values()) == pytest.approx(1.0, abs=1e-6)
    assert limited["cardinality"]["method"] == "greedy_top_n_resolve"
    assert limited["cardinality"]["requested"] == 3


def test_max_assets_reoptimizes_instead_of_renormalizing(synthetic_history, cardinality_user):
    """The kept weights are solved for the reduced universe, not rescaled.

    Truncate-and-renormalize would leave each survivor's share of the remaining
    weight exactly as it was in the 4-asset answer. A genuine re-solve moves it,
    because dropping a column changes the covariance the solver is minimizing.
    """
    universe = ["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"]
    kwargs = dict(
        scenario="risk_parity", current_weights=_weights_arg(),
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=universe,
    )
    full = optimize(constraints=_uncapped(), **kwargs)["target_weights"]
    limited = optimize(constraints=_uncapped(max_assets=3), **kwargs)["target_weights"]

    kept = set(limited)
    assert kept < set(full)
    scale = sum(full[k] for k in kept)
    renormalized = {k: full[k] / scale for k in kept}
    assert any(
        abs(limited[k] - renormalized[k]) > 1e-4 for k in kept
    ), "weights match a pure renormalization -- the second solve did not run"


def test_max_assets_below_the_floor_is_clamped(synthetic_history, cardinality_user):
    """Two assets is not a portfolio; the request is raised to MIN_CARDINALITY."""
    from portfolio.services.optimization import MIN_CARDINALITY

    result = optimize(
        scenario="min_volatility", current_weights=_weights_arg(),
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"],
        constraints=_uncapped(max_assets=1),
    )
    assert result["cardinality"]["requested"] == 1
    assert result["cardinality"]["limit"] == MIN_CARDINALITY
    assert len(result["target_weights"]) <= MIN_CARDINALITY


def test_max_assets_versions_the_cache(synthetic_history, cardinality_user):
    """Two different caps must not collide on one cache entry."""
    universe = ["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"]
    kwargs = dict(
        scenario="risk_parity", current_weights=_weights_arg(),
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=universe,
    )
    three = optimize(constraints=_uncapped(max_assets=3), **kwargs)
    unlimited = optimize(constraints=_uncapped(), **kwargs)

    assert unlimited["cached"] is False, "the uncapped run served the capped payload"
    assert unlimited["cardinality"] is None
    assert len(unlimited["target_weights"]) > len(three["target_weights"])


def test_max_assets_keeps_the_full_answer_when_caps_make_it_infeasible(
    synthetic_history, cardinality_user
):
    """Caps that cannot fill 100% from N assets degrade, never 500.

    Three assets under a 30% per-asset cap reach 0.90, so the reduced solve is
    arithmetically infeasible at full investment. The user asked for a
    preference, not an invariant: say it could not be honoured and hand back the
    unrestricted portfolio rather than raising SolverError at them.
    """
    universe = ["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"]
    result = optimize(
        scenario="risk_parity", current_weights=_weights_arg(),
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=universe,
        constraints=_uncapped(max_assets=3, max_weight_per_asset=0.30),
    )
    assert "cardinality_infeasible" in result["degraded"]
    assert len(result["target_weights"]) == 4, "the unrestricted answer is kept"
    assert sum(result["target_weights"].values()) == pytest.approx(1.0, abs=1e-6)
    assert any("not applied" in line for line in result["limitations"])


def test_max_assets_does_not_apply_to_equal_weight(synthetic_history, cardinality_user):
    """Equal weight ranks nothing, so any "largest N" subset would be arbitrary."""
    result = optimize(
        scenario="equal_weight", current_weights=_weights_arg(),
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"],
        constraints=_uncapped(max_assets=3),
    )
    assert result["cardinality"]["method"] == "not_applicable"
    assert len(result["target_weights"]) == 4



def test_enforce_caps_places_slack_on_zero_weight_assets():
    """Slack must reach assets the solver emptied, not be dropped.

    Four assets under a 32% cap can be fully invested. The redistribution tilts
    toward assets that already carry weight, so the first pass fills only the
    non-empty ones and stops at 0.64 -- and because slack is recomputed from
    scratch each round, the residue used to vanish and the caller raised
    "constraints are infeasible at full investment" on a feasible problem.
    """
    capped = _enforce_caps(
        {"a": 0.9, "b": 0.1, "c": 0.0, "d": 0.0},
        max_weight_per_asset=0.32,
        max_weight_per_class={},
        class_map={},
    )
    assert sum(capped.values()) == pytest.approx(1.0, abs=1e-9)
    assert set(capped) == {"a", "b", "c", "d"}
    assert max(capped.values()) <= 0.32 + 1e-9


def test_enforce_caps_still_reports_genuinely_infeasible_caps():
    """The fix must not paper over caps that really cannot reach 1.0."""
    capped = _enforce_caps(
        {"a": 0.5, "b": 0.3, "c": 0.2},
        max_weight_per_asset=0.20,
        max_weight_per_class={},
        class_map={},
    )
    # Three assets at 20% each is 0.60, full stop.
    assert sum(capped.values()) == pytest.approx(0.60, abs=1e-9)


def test_my_optimal_rejects_an_out_of_range_max_assets(synthetic_history, make_user):
    """A bad cap is a 400, not a 500 -- `optimize()` coerces it with `int()`."""
    pro = make_user(email="my_optimal_cap_bad@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    client = _client(pro)
    for bad in ("abc", "2", "400", "-1"):
        resp = client.get(
            f"/api/optimization/my-optimal/?account={acct.id}&max_assets={bad}"
        )
        assert resp.status_code == 400, f"max_assets={bad} should be rejected"
        assert "max_assets" in resp.json()["detail"]


def test_my_optimal_max_assets_caps_every_window(synthetic_history, make_user):
    """The cap reaches each window's scenarios, and echoes back in the body."""
    pro = make_user(email="my_optimal_cap@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    acct.tracking_started_at = timezone.now() - timedelta(days=40)
    acct.save(update_fields=["tracking_started_at"])

    resp = _client(pro).get(
        f"/api/optimization/my-optimal/?account={acct.id}&max_assets=3"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_assets"] == 3
    assert body["max_assets_range"] == [3, 40]
    solved = 0
    for window in body["windows"]:
        for key in ("min_volatility", "max_sharpe", "risk_parity", "hrp", "min_cvar"):
            payload = window.get(key)
            if not payload:
                continue
            solved += 1
            if "cardinality_infeasible" in payload["degraded"]:
                continue
            assert len(payload["target_weights"]) <= 3, f"{window['label']}/{key}"
    assert solved, "fixture solved nothing, so the assertion above never ran"


def test_my_optimal_max_assets_does_not_reuse_the_uncapped_cache(
    synthetic_history, make_user
):
    """The cap keys the response cache; the second call must not serve the first."""
    pro = make_user(email="my_optimal_cap_cache@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    client = _client(pro)
    uncapped = client.get(f"/api/optimization/my-optimal/?account={acct.id}").json()
    capped = client.get(
        f"/api/optimization/my-optimal/?account={acct.id}&max_assets=3"
    ).json()
    assert uncapped["max_assets"] is None
    assert capped["max_assets"] == 3


# ---------------------------------------------------------------------------
# risk tolerance -- "I accept X% volatility, earn me the most inside it"
# ---------------------------------------------------------------------------


def test_efficient_risk_respects_a_reachable_ceiling(synthetic_history, cardinality_user):
    """A ceiling above the minimum-variance floor is honoured and reported met."""
    universe = ["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"]
    kwargs = dict(
        current_weights=_weights_arg(), total_value_tomans=Decimal("1000000000"),
        user=cardinality_user, universe=universe,
    )
    floor = optimize(scenario="min_volatility", constraints=_uncapped(), **kwargs)
    floor_vol = floor["target_metrics"]["annualized_volatility"]

    ceiling = floor_vol * 1.5
    result = optimize(
        scenario="efficient_risk",
        constraints=_uncapped(target_volatility=ceiling), **kwargs
    )
    rt = result["risk_target"]
    assert rt["met"] is True
    assert rt["requested"] == pytest.approx(ceiling)
    assert rt["achieved"] <= ceiling + 1e-3, "the ceiling was exceeded"
    # Buying risk has to buy return, or the scenario is pointless.
    assert (
        result["target_metrics"]["expected_return_annual"]
        >= floor["target_metrics"]["expected_return_annual"] - 1e-9
    )
    assert result["forecast_free"] is False


def test_efficient_risk_below_the_floor_degrades_to_minimum_variance(
    synthetic_history, cardinality_user
):
    """An unreachable ceiling answers with the calmest book, and says so.

    "The least risky portfolio your assets can make is 18%" is the useful reply
    to "I want 2%" -- an error is not.
    """
    universe = ["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"]
    kwargs = dict(
        current_weights=_weights_arg(), total_value_tomans=Decimal("1000000000"),
        user=cardinality_user, universe=universe,
    )
    # Derived, not hardcoded: this fixture's usd_cash rail is nearly flat, so
    # its minimum-variance floor is a fraction of a percent. Half of whatever
    # the floor actually is unreachable by construction.
    floor = optimize(scenario="min_volatility", constraints=_uncapped(), **kwargs)
    floor_vol = floor["target_metrics"]["annualized_volatility"]
    assert floor_vol > 0

    result = optimize(
        scenario="efficient_risk",
        constraints=_uncapped(target_volatility=floor_vol / 2), **kwargs
    )
    assert result["risk_target"]["met"] is False
    assert "target_volatility_below_minimum" in result["degraded"]
    assert sum(result["target_weights"].values()) == pytest.approx(1.0, abs=1e-6)
    assert any("calmest portfolio" in line for line in result["limitations"])
    assert result["risk_target"]["achieved"] == pytest.approx(floor_vol, abs=1e-6)


def test_efficient_risk_without_a_target_is_a_solver_error(
    synthetic_history, cardinality_user
):
    """The scenario cannot run on its own; it needs the number from the user."""
    from portfolio.services.optimization import SolverError

    with pytest.raises(SolverError, match="target_volatility"):
        optimize(
            scenario="efficient_risk", current_weights=_weights_arg(),
            total_value_tomans=Decimal("1000000000"), user=cardinality_user,
            universe=["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"],
            constraints=_uncapped(),
        )


def test_efficient_risk_combines_with_a_position_cap(synthetic_history, cardinality_user):
    """The two Q5 controls are independent and must compose."""
    result = optimize(
        scenario="efficient_risk", current_weights=_weights_arg(),
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"],
        constraints=_uncapped(target_volatility=0.60, max_assets=3),
    )
    assert result["risk_target"] is not None
    if "cardinality_infeasible" not in result["degraded"]:
        assert len(result["target_weights"]) <= 3


def test_my_optimal_risk_target_adds_a_scenario(synthetic_history, make_user):
    """The endpoint solves efficient_risk only when a ceiling is supplied."""
    pro = make_user(email="my_optimal_risk@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    client = _client(pro)

    plain = client.get(f"/api/optimization/my-optimal/?account={acct.id}").json()
    assert plain["target_volatility"] is None
    assert all("efficient_risk" not in w for w in plain["windows"])

    with_target = client.get(
        f"/api/optimization/my-optimal/?account={acct.id}&target_volatility=0.35"
    ).json()
    assert with_target["target_volatility"] == 0.35
    solved = [w for w in with_target["windows"] if w.get("efficient_risk")]
    assert solved, "no window solved the risk-target scenario"
    for window in solved:
        assert window["efficient_risk"]["risk_target"]["requested"] == 0.35


def test_my_optimal_rejects_an_out_of_range_target_volatility(
    synthetic_history, make_user
):
    pro = make_user(email="my_optimal_risk_bad@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    client = _client(pro)
    for bad in ("abc", "0", "9"):
        resp = client.get(
            f"/api/optimization/my-optimal/?account={acct.id}&target_volatility={bad}"
        )
        assert resp.status_code == 400, f"target_volatility={bad} should be rejected"
        assert "target_volatility" in resp.json()["detail"]


def test_robustness_endpoint_solves_the_risk_scenario(synthetic_history, make_user):
    """Resampling the tab the user chose must not 503 on the one they asked for."""
    pro = make_user(email="robustness_risk@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    client = _client(pro)
    base = f"/api/optimization/robustness/?account={acct.id}&scenario=efficient_risk"

    # Without the ceiling the scenario cannot solve -- that is the 503 branch.
    assert client.get(base).status_code == 503

    resp = client.get(f"{base}&target_volatility=0.35")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scenario"] == "efficient_risk"
    assert body["robustness"]["converged"] > 0
    for band in body["robustness"]["bands"].values():
        assert band["p05"] <= band["p95"]
        assert band["width"] == pytest.approx(band["p95"] - band["p05"], abs=1e-6)


def test_robustness_endpoint_rejects_a_bad_target_volatility(
    synthetic_history, make_user
):
    pro = make_user(email="robustness_risk_bad@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    resp = _client(pro).get(
        f"/api/optimization/robustness/?account={acct.id}&target_volatility=nope"
    )
    assert resp.status_code == 400
    assert "target_volatility" in resp.json()["detail"]


def test_max_assets_budgets_for_proxy_expansion(synthetic_history, cardinality_user):
    """The cap is spent in rows the reader sees, not in solver columns.

    A collapsed proxy group is one column and several holdings -- two Swiss bars
    priced off the same gold series. Budgeting the cap in columns let a 4-asset
    request render six rows, which reads as the cap being ignored.
    """
    Asset.objects.filter(
        key__in=["swiss_gold_bar_1g", "swiss_gold_bar_2_5g"]
    ).update(proxy_key="emami_coin")

    weights = {
        "emami_coin": 0.20, "swiss_gold_bar_1g": 0.10, "swiss_gold_bar_2_5g": 0.10,
        "bitcoin_usd": 0.20, "usd_cash": 0.20, "kama_stock": 0.20,
    }
    universe = list(weights)
    result = optimize(
        scenario="risk_parity", current_weights=weights,
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=universe, held_keys=frozenset(weights),
        constraints=_uncapped(max_assets=4),
    )
    assert result["proxy_groups"], "fixture did not produce a proxy group"
    card = result["cardinality"]
    assert card["positions"] == len(result["target_weights"])
    assert card["positions"] == card["chosen"] + card["frozen"]

    # The budget is spent in rows: a 6-holding book asked for 4 must not come
    # back with all 6 just because two of them collapsed onto one column.
    assert card["chosen"] < len(weights)
    # Three columns is the floor, and three columns can cost more than the limit
    # when one is a proxy group. Where that happens it is stated, not hidden.
    if "cardinality_floor_exceeds_limit" in result["degraded"]:
        assert card["met"] is False
        assert any("could not be met" in line for line in result["limitations"])
    else:
        assert card["chosen"] <= card["limit"], result["target_weights"]
        assert card["met"] is True


def test_cardinality_reports_frozen_holdings_separately(synthetic_history, cardinality_user):
    """A holding the solver cannot measure is counted, not hidden or sold."""
    Asset.objects.create(
        key="unmeasurable", name="Unmeasurable", asset_class="Gold", currency="IRT",
    )
    weights = {
        "emami_coin": 0.2, "bitcoin_usd": 0.2, "usd_cash": 0.2,
        "kama_stock": 0.2, "unmeasurable": 0.2,
    }
    result = optimize(
        scenario="risk_parity", current_weights=weights,
        total_value_tomans=Decimal("1000000000"), user=cardinality_user,
        universe=list(weights), held_keys=frozenset(weights),
        constraints=_uncapped(max_assets=3),
    )
    card = result["cardinality"]
    assert card["frozen"] >= 1
    assert "unmeasurable" in result["frozen_weights"]
    assert "unmeasurable" in result["target_weights"], "a frozen holding must not be sold"
    assert card["chosen"] <= 3
    assert any("held at their current weight" in line for line in result["limitations"])


def test_efficient_risk_met_is_measured_not_inferred(synthetic_history, cardinality_user):
    """`met` reads the achieved volatility, never the fallback flag.

    The scenario can land inside the ceiling by way of the minimum-variance
    fallback. That is a portfolio inside the budget, so it is met -- inferring
    the answer from `target_volatility_below_minimum` printed "35% is below what
    these assets can achieve" directly above an achieved 27%.
    """
    universe = ["emami_coin", "bitcoin_usd", "usd_cash", "kama_stock"]
    kwargs = dict(
        current_weights=_weights_arg(), total_value_tomans=Decimal("1000000000"),
        user=cardinality_user, universe=universe,
    )
    floor = optimize(scenario="min_volatility", constraints=_uncapped(), **kwargs)
    floor_vol = floor["target_metrics"]["annualized_volatility"]

    result = optimize(
        scenario="efficient_risk",
        constraints=_uncapped(target_volatility=floor_vol * 3), **kwargs
    )
    rt = result["risk_target"]
    assert rt["achieved"] <= rt["requested"] + 1e-6
    assert rt["met"] is True
    # And the contradiction that motivated this test must be impossible.
    assert not (rt["met"] and any("calmest portfolio" in l for l in result["limitations"]))


def test_efficient_risk_relaxes_the_hard_asset_sleeve(asset_catalog, db):
    """A gold+cash book breaches the sleeve on day one; the ladder must survive it.

    Without the sleeve rung the scenario failed every attempt, fell through to
    minimum variance, and then reported every ceiling as unreachable.
    """
    from portfolio.services.optimization import _efficient_risk

    rng = np.random.default_rng(5)
    days = 90
    idx = pd.date_range("2025-01-01", periods=days, freq="D")
    returns = pd.DataFrame({
        "emami_coin": rng.normal(0.002, 0.012, days),
        "gold_18k_gram": rng.normal(0.002, 0.011, days),
        "usd_cash": rng.normal(0.001, 0.009, days),
        "kama_stock": rng.normal(0.001, 0.020, days),
    }, index=idx)
    cov = returns.cov()
    class_map = {
        "emami_coin": "Gold", "gold_18k_gram": "Gold",
        "usd_cash": "Cash", "kama_stock": "Stock",
    }
    degraded = []
    weights = _efficient_risk(
        returns, cov,
        max_weight_per_asset=0.40,
        max_weight_per_class={"Gold": 0.60, "Cash": 0.80, "Stock": 0.50},
        class_map=class_map,
        periods_per_year=365.0,
        degraded=degraded,
        mu_daily=returns.mean(),
        target_volatility=0.30,
        # A sleeve tight enough that gold + cash cannot satisfy it.
        sleeves=[{
            "id": "hard_assets", "assets": ["emami_coin", "gold_18k_gram", "usd_cash"],
            "max_combined_weight": 0.05,
        }],
    )
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-4)
    assert "target_volatility_below_minimum" not in degraded, (
        "fell through to minimum variance instead of relaxing the sleeve"
    )


def test_my_optimal_serves_snapshot_and_triggers_refresh_when_stale(
    synthetic_history, make_user, monkeypatch
):
    """Integration: MyOptimal snapshot serving, stale refresh trigger, and as_of inclusion."""
    from unittest.mock import MagicMock
    from portfolio.optimization_models import OptimizationSnapshot

    pro = make_user(email="snap_test@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 1, "bitcoin_usd": 1, "usd_cash": 1000},
    )

    # First request: cold start computes inline and creates OptimizationSnapshot
    client = _client(pro)
    resp1 = client.get(f"/api/optimization/my-optimal/?account={acct.id}")
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert "as_of" in data1

    snap = OptimizationSnapshot.objects.filter(
        account=acct, scenario="my_optimal", basis="real_toman"
    ).first()
    assert snap is not None
    assert snap.basis == "real_toman"

    # Make the snapshot appear 20 minutes old
    snap.created_at = timezone.now() - timedelta(minutes=20)
    snap.save(update_fields=["created_at"])

    # Clear memory cache so view is forced to hit the snapshot DB layer
    cache.clear()

    # Second request: served from snapshot, triggers background refresh
    refresh_mock = MagicMock()
    monkeypatch.setattr("portfolio.tasks.refresh_my_optimal_snapshot.delay", refresh_mock)

    resp2 = client.get(f"/api/optimization/my-optimal/?account={acct.id}")
    assert resp2.status_code == 200
    assert resp2.json()["windows"] == data1["windows"]
    assert refresh_mock.called


def test_my_optimal_knobs_bypass_snapshot(synthetic_history, make_user):
    """Integration: custom knob values compute live instead of serving default snapshot."""
    from portfolio.optimization_models import OptimizationSnapshot

    pro = make_user(email="knobs_test@t.t")
    acct = _make_portfolio(
        pro, synthetic_history,
        {"emami_coin": 1, "bitcoin_usd": 1, "usd_cash": 1000},
    )
    client = _client(pro)

    # Seed a snapshot
    resp1 = client.get(f"/api/optimization/my-optimal/?account={acct.id}")
    assert resp1.status_code == 200

    # Request with max_assets knob: bypasses snapshot and sets max_assets
    resp2 = client.get(f"/api/optimization/my-optimal/?account={acct.id}&max_assets=3")
    assert resp2.status_code == 200
    assert resp2.json()["max_assets"] == 3


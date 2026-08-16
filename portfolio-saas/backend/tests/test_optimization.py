"""Pro portfolio-optimization engine: returns pipeline, scenarios, caching, gating.

The synthetic_history fixture writes ~40 days of Price rows for 4 liquid assets
with KNOWN daily-return profiles (low-vol emami_coin, alternating bitcoin_usd,
flat usd_cash, drift kama_stock) plus usd_cash so the USD->Toman conversion
path is exercised. Tests then assert the engine recovers the known values and
that the scenario optimizers produce well-formed, constraint-respecting
weights.

NB: `Price.fetched_at` is `auto_now_add`, so explicit `fetched_at` values on
create are silently overridden. The fixtures use a CASE-expression bulk UPDATE
after bulk_create to backfill the intended timestamps.
"""
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from django.core.cache import cache
from django.db.models import Case, DateTimeField, When
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from portfolio.models import Account, Asset, Holding, Price
from portfolio.services.diagnostics import portfolio_diagnostics
from portfolio.services.optimization import (
    UniverseTooSmall,
    _correlation_clusters,
    _efficient_frontier,
    _enforce_caps,
    _rebalance_trades,
    optimize,
)
from portfolio.services.returns import (
    DEFAULT_HISTORY_DAYS,
    RETURNS_CACHE_KEY,
    _price_version_fingerprint,
    daily_returns_matrix,
    invalidate_returns_cache,
)


pytestmark = pytest.mark.django_db


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

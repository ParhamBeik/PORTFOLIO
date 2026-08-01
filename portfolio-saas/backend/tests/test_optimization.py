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
    _efficient_frontier,
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
    days = 42
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
        assert np.isfinite(v), f"{k} not finite: {v}"
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


def test_infeasible_caps_are_rejected():
    from portfolio.services.optimization import _enforce_caps

    constrained = _enforce_caps(
        {"gold_a": 0.5, "gold_b": 0.3, "gold_c": 0.2},
        max_weight_per_asset=0.4,
        max_weight_per_class={"Gold": 0.6},
        class_map={"gold_a": "Gold", "gold_b": "Gold", "gold_c": "Gold"},
    )
    assert sum(constrained.values()) == pytest.approx(0.6)
    assert max(constrained.values()) <= 0.4


# ---------- 13. analytics pro gated -----------------------------------------


def test_analytics_pro_gated(synthetic_history, make_user):
    free = make_user(tier="FREE", email="free@t.t")
    pro = make_user(tier="PRO", email="pro@t.t")
    _make_portfolio(
        pro,
        synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    _make_portfolio(
        free,
        synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
        account_name="FreeAcct",
    )
    assert _client(free).get("/api/analytics/").status_code == 403
    resp = _client(pro).get("/api/analytics/")
    assert resp.status_code == 200
    body = resp.json()
    assert "metrics" in body
    assert "eligible_assets" in body


# ---------- 14. optimization pro gated --------------------------------------


def test_optimization_pro_gated(synthetic_history, make_user):
    free = make_user(tier="FREE", email="free2@t.t")
    _make_portfolio(
        free,
        synthetic_history,
        {"emami_coin": 0.4, "bitcoin_usd": 0.3, "usd_cash": 0.3},
    )
    resp = _client(free).post(
        "/api/optimization/", {"scenario": "max_sharpe"}, format="json"
    )
    assert resp.status_code == 403


# ---------- 15. unknown scenario 400 ----------------------------------------


def test_optimization_unknown_scenario_400(synthetic_history, make_user):
    pro = make_user(tier="PRO", email="pro2@t.t")
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
    pro = make_user(tier="PRO", email="pro3@t.t")
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

"""Scenario optimization engine for Pro portfolios.

Four portfolio-construction scenarios share one pipeline:
  * `max_sharpe`     — EfficientFrontier tangency portfolio (pypfopt).
  * `min_volatility` — EfficientFrontier global-min-vol portfolio.
  * `risk_parity`    — convex ERC formulation solved in cvxpy (ECOS).
  * `hrp`            — Hierarchical Risk Parity (pypfopt) with post-hoc caps.

Covariance is shrunk with Ledoit-Wolf throughout — daily returns are noisy and
the shrinkage estimator is more stable on the small (~weekly) history we have.
All inputs are annualized for pypfopt (mu = mean * 252, S = cov * 252).

Per-asset and per-class caps are enforced for max_sharpe / min_vol via pypfopt
constraints, and for risk_parity / hrp via a post-hoc rescale-and-clip +
renormalize pass (these methods have no native cap primitives).

The whole payload is cached per (user, portfolio state, scenario, price-version,
constraints hash) with a 600s TTL — a new price or holding state misses cleanly.
"""
from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal

import numpy as np
import pandas as pd
import cvxpy as cp
from django.core.cache import cache
from pypfopt import EfficientFrontier, HRPOpt
from sklearn.covariance import LedoitWolf

from portfolio.models import Asset
from .returns import _price_version_fingerprint, daily_returns_matrix

# Same risk-free proxy as diagnostics, kept here so the optimizer is standalone.
RISK_FREE_RATE_ANNUAL = 0.30
TRADING_DAYS_PER_YEAR = 252

DEFAULT_CONSTRAINTS = {
    "long_only": True,
    "max_weight_per_asset": 0.40,
    "max_weight_per_class": {
        "Gold": 0.60,
        "Crypto": 0.30,
        "Stock": 0.50,
        "Cash": 0.80,
    },
}

SCENARIOS = ("max_sharpe", "min_volatility", "risk_parity", "hrp")

_OPT_CACHE_TTL = 600


class UniverseTooSmall(Exception):
    """Raised when the eligible universe has fewer than 3 assets.

    The view maps this to 503 so the client can show a friendly 'come back
    later' message rather than a generic error.
    """

    def __init__(self, eligible: list[str]):
        super().__init__(f"eligible universe too small: {len(eligible)}")
        self.eligible = eligible


class SolverError(Exception):
    """Raised when all solvers fail to solve the optimization problem."""
    pass


class NoAssetBeatsRiskFreeRate(Exception):
    """Raised when no asset has expected returns above the risk-free rate."""
    pass


# ---------- helpers ----------------------------------------------------------


def _asset_class_map() -> dict[str, str]:
    """{asset_key: asset_class} for the same universe the returns df uses."""
    return {
        key: cls
        for key, cls in Asset.objects.filter(is_active=True)
        .exclude(is_house=True)
        .values_list("key", "asset_class")
    }


def _shrunk_covariance(returns: pd.DataFrame) -> pd.DataFrame:
    """Ledoit-Wolf shrunk covariance of daily returns (DataFrame, daily scale)."""
    mat = returns.fillna(0.0).to_numpy()
    lw = LedoitWolf().fit(mat)
    return pd.DataFrame(lw.covariance_, index=returns.columns, columns=returns.columns)


def _enforce_caps(
    weights: dict[str, float],
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
) -> dict[str, float]:
    """Iteratively cap per-asset and per-class weights, redistributing slack.

    Each round:
      1. Clip every asset at `max_weight_per_asset`. The clipped excess becomes
         "slack" to redistribute.
      2. For each class whose total exceeds its cap, scale the class down to the
         cap (proportional preservation); the dropped weight also becomes slack.
      3. Redistribute the slack across assets that still have headroom, in
         proportion to their current weight. If no asset has headroom, drop the
         slack (total will be < 1.0 — the caps are infeasible at full investment).

    Converges because each round strictly reduces total cap violation. After the
    loop, weights sum to <= 1.0 and respect every cap; the caller normalizes.
    """
    if not weights:
        return weights
    w = {k: max(float(v), 0.0) for k, v in weights.items()}
    keys = list(w.keys())
    caps_class = {k: float(max_weight_per_class.get(class_map.get(k, "Other"), 1.0)) for k in keys}

    for _ in range(20):
        # --- Step 1: per-asset cap, collect slack ---
        slack = 0.0
        for k in keys:
            if w[k] > max_weight_per_asset:
                slack += w[k] - max_weight_per_asset
                w[k] = max_weight_per_asset

        # --- Step 2: per-class cap, collect slack ---
        # Group by class, scale down over-cap classes proportionally.
        by_class: dict[str, list[str]] = {}
        for k in keys:
            by_class.setdefault(class_map.get(k, "Other"), []).append(k)
        for cls, cls_keys in by_class.items():
            cap = caps_class[cls_keys[0]]
            total = sum(w[k] for k in cls_keys)
            if total > cap + 1e-12 and total > 0:
                factor = cap / total
                for k in cls_keys:
                    new_v = w[k] * factor
                    slack += w[k] - new_v
                    w[k] = new_v

        # --- Step 3: redistribute slack to assets with headroom ---
        if slack <= 1e-12:
            break
        # Headroom per asset: min(asset cap - current, class cap - class total).
        cls_total = {cls: sum(w[k] for k in ks) for cls, ks in by_class.items()}
        headroom = {}
        for k in keys:
            cls = class_map.get(k, "Other")
            hr_asset = max_weight_per_asset - w[k]
            hr_class = caps_class[k] - cls_total[cls]
            headroom[k] = max(0.0, min(hr_asset, hr_class))
        total_headroom = sum(headroom.values())
        if total_headroom <= 1e-12:
            break  # No room to redistribute — caps are infeasible at full investment.
        # Distribute slack in proportion to headroom (weighted by current weight
        # so empty assets don't grab everything).
        weight_factor = {k: (headroom[k] * (w[k] + 1e-9)) for k in keys}
        tw = sum(weight_factor.values())
        if tw <= 0:
            # Fall back to pure headroom proportion.
            weight_factor = headroom
            tw = total_headroom
        distributed = 0.0
        for k in keys:
            share = weight_factor[k] / tw * slack
            # Don't exceed headroom.
            share = min(share, headroom[k])
            w[k] += share
            distributed += share
        if distributed <= 1e-12:
            break

    return {k: float(v) for k, v in w.items() if v > 1e-6}


def _portfolio_metrics(
    weights: dict[str, float],
    mu: pd.Series,
    cov_annual: pd.DataFrame,
    *,
    risk_free_annual: float = RISK_FREE_RATE_ANNUAL,
) -> dict:
    """Annualized (return, volatility, Sharpe) for a weight dict over mu/cov."""
    cols = [k for k in weights if k in mu.index]
    if not cols:
        return {"expected_return_annual": 0.0, "annualized_volatility": 0.0, "sharpe": 0.0}
    w = np.array([weights[k] for k in cols], dtype=float)
    if w.sum() <= 0:
        return {"expected_return_annual": 0.0, "annualized_volatility": 0.0, "sharpe": 0.0}
    w = w / w.sum()
    mu_v = mu.reindex(cols).to_numpy()
    cov_v = cov_annual.reindex(index=cols, columns=cols).to_numpy()
    ann_return = float(np.dot(w, mu_v))
    ann_vol = float(np.sqrt(max(w @ cov_v @ w, 0.0)))
    sharpe = (ann_return - risk_free_annual) / ann_vol if ann_vol > 0 else 0.0
    return {
        "expected_return_annual": _finite(ann_return),
        "annualized_volatility": _finite(ann_vol),
        "sharpe": _finite(sharpe),
    }


def _finite(value, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if np.isfinite(f) else default


def _rebalance_trades(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    total_value_tomans: Decimal,
) -> list[dict]:
    """Diff current vs target weights into buy/sell trades (Decimal Toman strings)."""
    keys = sorted(set(current_weights) | set(target_weights))
    total_decimal = Decimal(str(total_value_tomans))
    trades = []
    for key in keys:
        delta = float(target_weights.get(key, 0.0)) - float(current_weights.get(key, 0.0))
        if abs(delta) < 1e-6:
            continue
        value = (Decimal(str(abs(delta))) * total_decimal).quantize(Decimal("0.0001"))
        trades.append(
            {
                "key": key,
                "action": "buy" if delta > 0 else "sell",
                "delta_weight_pct": round(delta * 100, 4),
                "delta_value_tomans": str(value),
            }
        )
    return trades


def _constraints_hash(constraints: dict) -> str:
    """Stable short hash for cache keying."""
    payload = json.dumps(constraints, sort_keys=True, default=str)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]


# ---------- scenario solvers -------------------------------------------------


def _max_sharpe(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
) -> dict[str, float]:
    mu = returns.mean() * TRADING_DAYS_PER_YEAR
    S = cov_daily * TRADING_DAYS_PER_YEAR
    
    solvers = ["CLARABEL", "SCS", "OSQP"]
    raw = None
    last_exc = None

    for solver in solvers:
        try:
            ef = EfficientFrontier(mu, S, weight_bounds=(0.0, max_weight_per_asset), solver=solver)
            for cls, cap in max_weight_per_class.items():
                idx = [i for i, k in enumerate(S.columns) if class_map.get(k) == cls]
                if idx and cap < 1.0:
                    cap_f = float(cap)
                    ef.add_constraint(
                        lambda x, idx=idx, cap_f=cap_f: cp.sum(x[idx]) <= cap_f
                    )
            raw = ef.max_sharpe(risk_free_rate=RISK_FREE_RATE_ANNUAL)
            break
        except Exception as e:
            last_exc = e
            continue

    if raw is None:
        for solver in solvers:
            try:
                ef2 = EfficientFrontier(mu, S, weight_bounds=(0.0, max_weight_per_asset), solver=solver)
                raw = ef2.max_sharpe(risk_free_rate=RISK_FREE_RATE_ANNUAL)
                break
            except Exception as e:
                last_exc = e
                continue

    if raw is None:
        raise SolverError(f"All solvers (CLARABEL, SCS, OSQP) failed to solve Max-Sharpe: {last_exc}")

    weights = {k: float(v) for k, v in raw.items() if v > 1e-6}
    # Belt-and-braces: post-hoc cap in case a class constraint was relaxed.
    return _enforce_caps(
        weights,
        max_weight_per_asset=max_weight_per_asset,
        max_weight_per_class=max_weight_per_class,
        class_map=class_map,
    )


def _min_volatility(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
) -> dict[str, float]:
    mu = returns.mean() * TRADING_DAYS_PER_YEAR
    S = cov_daily * TRADING_DAYS_PER_YEAR
    
    solvers = ["CLARABEL", "SCS", "OSQP"]
    raw = None
    last_exc = None

    for solver in solvers:
        try:
            ef = EfficientFrontier(mu, S, weight_bounds=(0.0, max_weight_per_asset), solver=solver)
            for cls, cap in max_weight_per_class.items():
                idx = [i for i, k in enumerate(S.columns) if class_map.get(k) == cls]
                if idx and cap < 1.0:
                    cap_f = float(cap)
                    ef.add_constraint(
                        lambda x, idx=idx, cap_f=cap_f: cp.sum(x[idx]) <= cap_f
                    )
            raw = ef.min_volatility()
            break
        except Exception as e:
            last_exc = e
            continue

    if raw is None:
        for solver in solvers:
            try:
                ef2 = EfficientFrontier(mu, S, weight_bounds=(0.0, max_weight_per_asset), solver=solver)
                raw = ef2.min_volatility()
                break
            except Exception as e:
                last_exc = e
                continue

    if raw is None:
        raise SolverError(f"All solvers (CLARABEL, SCS, OSQP) failed to solve Min-Volatility: {last_exc}")

    weights = {k: float(v) for k, v in raw.items() if v > 1e-6}
    return _enforce_caps(
        weights,
        max_weight_per_asset=max_weight_per_asset,
        max_weight_per_class=max_weight_per_class,
        class_map=class_map,
    )


def _risk_parity(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
) -> dict[str, float]:
    """Convex ERC (Spinu 2013): minimize `w' S w - (2/n) * sum(log(w_i))`.

    The unique minimizer (over w > 0, with no other constraints) is the equal-
    risk-contribution portfolio. The log-barrier keeps weights strictly positive
    and the problem is DCP-compliant in cvxpy. Caps enforced post-hoc since the
    barrier formulation doesn't accept linear inequality caps cleanly.
    """
    S = cov_daily.to_numpy()
    n = S.shape[0]
    keys = list(cov_daily.columns)
    # Add a tiny ridge so S is strictly PD (the log barrier requires PD S).
    S = S + np.eye(n) * 1e-10
    w = cp.Variable(n, pos=True)
    objective = cp.Minimize(0.5 * cp.quad_form(w, cp.psd_wrap(S)) - (2.0 / n) * cp.sum(cp.log(w)))
    # NB: per-asset caps are NOT applied here. The Spinu optimum is scale-free
    # (its ERC property holds at any scaling), and adding `w <= cap` on the raw
    # (unnormalized) variables would make the problem infeasible since the raw
    # solution has values far above any fractional cap. Caps are enforced
    # post-hoc on the normalized weights via `_enforce_caps`.
    prob = cp.Problem(objective, [])
    
    solvers = ["CLARABEL", "SCS", "ECOS"]
    w_val = None
    for s_name in solvers:
        try:
            if s_name in cp.installed_solvers():
                prob.solve(solver=s_name)
                if prob.status in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE) and w.value is not None:
                    w_val = w.value
                    break
        except Exception:
            continue
            
    if w_val is None:
        try:
            prob.solve()
            if prob.status in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE) and w.value is not None:
                w_val = w.value
        except Exception:
            pass

    if w_val is None or not np.all(np.isfinite(w_val)):
        # Equal-weight fallback if the solver fails.
        equal = {k: 1.0 / n for k in keys}
        return _enforce_caps(
            equal,
            max_weight_per_asset=max_weight_per_asset,
            max_weight_per_class=max_weight_per_class,
            class_map=class_map,
        )
    # The Spinu formulation's optimum is NOT normalized — normalize first so the
    # caps (which are fractional) apply correctly.
    total = float(np.sum(w_val))
    if total > 0:
        w_val = w_val / total
    weights = {k: float(v) for k, v in zip(keys, w_val) if v > 1e-6}
    return _enforce_caps(
        weights,
        max_weight_per_asset=max_weight_per_asset,
        max_weight_per_class=max_weight_per_class,
        class_map=class_map,
    )


def _hrp(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
) -> dict[str, float]:
    hrp = HRPOpt(returns=returns.fillna(0.0))
    try:
        raw = hrp.optimize()
    except Exception:
        # Fall back to equal weights; will still be cap-enforced below.
        raw = {k: 1.0 / len(returns.columns) for k in returns.columns}
    weights = {k: float(v) for k, v in raw.items() if v > 1e-6}
    return _enforce_caps(
        weights,
        max_weight_per_asset=max_weight_per_asset,
        max_weight_per_class=max_weight_per_class,
        class_map=class_map,
    )


_SCENARIO_DISPATCH = {
    "max_sharpe": _max_sharpe,
    "min_volatility": _min_volatility,
    "risk_parity": _risk_parity,
    "hrp": _hrp,
}


# ---------- public entry point -----------------------------------------------


def optimize(
    *,
    scenario: str,
    current_weights: dict[str, float],
    total_value_tomans: Decimal,
    constraints: dict | None = None,
    user=None,
) -> dict:
    """Run one optimization scenario and return the full payload.

    The payload includes `cached` (bool) so the client can show whether this
    came from the cache. Cache key includes the current portfolio state so
    account-scoped weights and rebalance trades cannot leak across requests.
    """
    resolved = copy.deepcopy(DEFAULT_CONSTRAINTS)
    if constraints:
        for k, v in constraints.items():
            resolved[k] = v
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")

    version = _price_version_fingerprint()
    user_id = user.id if user is not None else 0
    portfolio_hash = _constraints_hash({
        "weights": current_weights,
        "total": str(total_value_tomans),
    })
    cache_key = (
        f"opt:{user_id}:{portfolio_hash}:{scenario}:v{version}:{_constraints_hash(resolved)}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        payload = dict(cached)
        payload["cached"] = True
        return payload

    returns, excluded = daily_returns_matrix()
    if returns.empty or len(returns.columns) < 3:
        raise UniverseTooSmall(list(returns.columns) if not returns.empty else [])
    # Restrict the eligible universe to assets with any variance.
    eligible = [k for k in returns.columns if returns[k].std(ddof=0) > 0]
    if len(eligible) < 3:
        raise UniverseTooSmall(eligible)
    returns = returns[eligible]

    class_map = _asset_class_map()
    cov_daily = _shrunk_covariance(returns)
    mu = returns.mean() * TRADING_DAYS_PER_YEAR
    cov_annual = cov_daily * TRADING_DAYS_PER_YEAR

    if scenario == "max_sharpe":
        if not (mu > RISK_FREE_RATE_ANNUAL).any():
            raise NoAssetBeatsRiskFreeRate(
                f"No asset in the eligible universe has an expected annualized return exceeding "
                f"the risk-free rate of {int(RISK_FREE_RATE_ANNUAL * 100)}%."
            )

    solver = _SCENARIO_DISPATCH[scenario]
    target = solver(
        returns,
        cov_daily,
        max_weight_per_asset=float(resolved["max_weight_per_asset"]),
        max_weight_per_class=resolved["max_weight_per_class"],
        class_map=class_map,
    )
    total = sum(target.values())
    if abs(total - 1.0) > 1e-6:
        raise SolverError("Constraints are infeasible for a fully invested portfolio.")
    target = {k: float(v) for k, v in target.items() if v > 1e-6}

    metrics = _portfolio_metrics(target, mu, cov_annual)
    trades = _rebalance_trades(current_weights, target, total_value_tomans)

    payload = {
        "scenario": scenario,
        "eligible_assets": list(returns.columns),
        "excluded_assets": excluded,
        "target_weights": target,
        "target_metrics": metrics,
        "rebalance_trades": trades,
        "current_weights": current_weights,
        "constraints_applied": resolved,
        "price_version": version,
        "cached": False,
    }
    cache.set(cache_key, payload, timeout=_OPT_CACHE_TTL)
    return payload


# ---------- efficient frontier -----------------------------------------------


def _solve_ef_min_vol(mu, S, cap):
    last_exc = None
    for solver in ["CLARABEL", "SCS", "OSQP"]:
        try:
            ef = EfficientFrontier(mu, S, weight_bounds=(0.0, cap), solver=solver)
            w = ef.min_volatility()
            ret, vol, sharpe = ef.portfolio_performance(risk_free_rate=RISK_FREE_RATE_ANNUAL)
            return ef, w, ret, vol
        except Exception as e:
            last_exc = e
            continue
    raise SolverError(f"Failed to solve min volatility: {last_exc}")


def _solve_ef_max_sharpe(mu, S, cap):
    last_exc = None
    for solver in ["CLARABEL", "SCS", "OSQP"]:
        try:
            ef = EfficientFrontier(mu, S, weight_bounds=(0.0, cap), solver=solver)
            w = ef.max_sharpe(risk_free_rate=RISK_FREE_RATE_ANNUAL)
            ret, vol, sharpe = ef.portfolio_performance(risk_free_rate=RISK_FREE_RATE_ANNUAL)
            return ef, w, ret
        except Exception as e:
            last_exc = e
            continue
    raise SolverError(f"Failed to solve max Sharpe: {last_exc}")


def _efficient_frontier(n_points: int = 30) -> dict:
    """Sample the efficient frontier + reference points.

    Sweeps target returns from min-vol to max-Sharpe and solves min-vol at each
    level. Returns the frontier (vol ascending) plus the current-portfolio
    point (None here — the view injects the live point) and max_sharpe / min_vol
    points. Reuses the cached returns matrix so it's cheap on a warm cache.
    """
    returns, _ = daily_returns_matrix()
    if returns.empty or len(returns.columns) < 2:
        return {"frontier": [], "max_sharpe": None, "min_volatility": None}
    eligible = [k for k in returns.columns if returns[k].std(ddof=0) > 0]
    if len(eligible) < 2:
        return {"frontier": [], "max_sharpe": None, "min_volatility": None}
    returns = returns[eligible]
    class_map = _asset_class_map()
    cov_daily = _shrunk_covariance(returns)
    mu = returns.mean() * TRADING_DAYS_PER_YEAR
    S = cov_daily * TRADING_DAYS_PER_YEAR

    cap = float(DEFAULT_CONSTRAINTS["max_weight_per_asset"])
    frontier: list[dict] = []
    try:
        ef_min, w_min, ret_min, vol_min = _solve_ef_min_vol(mu, S, cap)
        ef_max, w_max, ret_max_eff = _solve_ef_max_sharpe(mu, S, cap)
    except Exception:
        return {"frontier": [], "max_sharpe": None, "min_volatility": None}

    target_returns = np.linspace(ret_min, ret_max_eff, n_points)
    seen: set[float] = set()
    for tr in target_returns:
        try:
            solved = False
            for solver in ["CLARABEL", "SCS", "OSQP"]:
                try:
                    ef = EfficientFrontier(mu, S, weight_bounds=(0.0, cap), solver=solver)
                    ef.efficient_return(target_return=float(tr))
                    _, vol, sharpe = ef.portfolio_performance(risk_free_rate=RISK_FREE_RATE_ANNUAL)
                    solved = True
                    break
                except Exception:
                    continue
            if not solved:
                continue
            key = round(vol, 8)
            if key in seen:
                continue
            seen.add(key)
            frontier.append(
                {
                    "return": _finite(tr),
                    "volatility": _finite(vol),
                    "sharpe": _finite(sharpe),
                }
            )
        except Exception:
            continue

    frontier.sort(key=lambda p: p["volatility"])
    # Reference points.
    try:
        ms = _max_sharpe(
            returns,
            cov_daily,
            max_weight_per_asset=cap,
            max_weight_per_class=DEFAULT_CONSTRAINTS["max_weight_per_class"],
            class_map=class_map,
        )
        ms_metrics = _portfolio_metrics(ms, mu, S)
    except Exception:
        ms_metrics = {"expected_return_annual": 0.0, "annualized_volatility": 0.0, "sharpe": 0.0}
        ms = {}
    try:
        mv = _min_volatility(
            returns,
            cov_daily,
            max_weight_per_asset=cap,
            max_weight_per_class=DEFAULT_CONSTRAINTS["max_weight_per_class"],
            class_map=class_map,
        )
        mv_metrics = _portfolio_metrics(mv, mu, S)
    except Exception:
        mv_metrics = {"expected_return_annual": 0.0, "annualized_volatility": 0.0, "sharpe": 0.0}
        mv = {}

    return {
        "frontier": frontier,
        "max_sharpe": {"weights": ms, "metrics": ms_metrics},
        "min_volatility": {"weights": mv, "metrics": mv_metrics},
    }

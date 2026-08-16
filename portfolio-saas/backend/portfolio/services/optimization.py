"""Scenario optimization engine for Pro portfolios.

Five portfolio-construction scenarios share one pipeline:
  * `equal_weight`   — transparent baseline with one equal sleeve per asset.
  * `max_sharpe`     — EfficientFrontier tangency portfolio (pypfopt).
  * `min_volatility` — EfficientFrontier global-min-vol portfolio.
  * `risk_parity`    — convex ERC formulation solved in cvxpy (ECOS).
  * `hrp`            — Hierarchical Risk Parity (pypfopt) with post-hoc caps.

Covariance is shrunk with Ledoit-Wolf throughout — daily returns are noisy and
the shrinkage estimator is more stable on the small (~weekly) history we have.
All inputs are annualized for pypfopt (mu = mean * 252, S = cov * 252).

Per-asset, per-class, and Gold+Cash sleeve caps are enforced for max_sharpe /
min_vol via pypfopt constraints, and for risk_parity / hrp via a post-hoc
rescale-and-clip + renormalize pass (these methods have no native cap primitives).

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

from .classification import HARD_ASSET_SLEEVE, asset_class_map, class_totals
from .deflator import normalize_basis
from .returns import MIN_DAILY_RETURNS, _price_version_fingerprint, daily_returns_matrix

# Same risk-free proxy as diagnostics, kept here so the optimizer is standalone.
from django.conf import settings
RISK_FREE_RATE_ANNUAL = float(getattr(settings, "RISK_FREE_RATE_ANNUAL", 0.30))
TRADING_DAYS_PER_YEAR = 252

# Below this, the shared observation window is being asked to pin down more
# covariance parameters than it has data for -- Ledoit-Wolf will still invert
# the matrix, but the result is fit to noise (a 200x200 covariance from 45
# rows produced a live Sharpe of 16). `optimize()` enforces
# len(shared_window) >= MIN_OBSERVATIONS_PER_ASSET * n_assets, dropping the
# shallowest-history assets to get there rather than reporting the number.
MIN_OBSERVATIONS_PER_ASSET = 10

# A result outside these bounds isn't a portfolio, it's overfit noise dressed
# up with a solver. Never suppressed -- surfaced via payload["credibility"].
SHARPE_CREDIBILITY_CEILING = 3.0
EXPECTED_RETURN_CREDIBILITY_CEILING = 1.0  # 100%/yr

DEFAULT_CONSTRAINTS = {
    "long_only": True,
    "max_weight_per_asset": 0.40,
    "max_weight_per_class": {
        "Gold": 0.60,
        "Crypto": 0.30,
        "Stock": 0.50,
        "Cash": 0.80,
    },
    # Assets whose returns correlate above this threshold are grouped; their
    # combined weight is capped so the optimizer cannot pile into USD + gold
    # (or multiple gold coins) that move together.
    "correlation_cluster_threshold": 0.65,
    "max_weight_per_correlation_cluster": 0.50,
    "sleeves": [
        {
            "id": HARD_ASSET_SLEEVE["id"],
            "classes": list(HARD_ASSET_SLEEVE["classes"]),
            "max_weight": HARD_ASSET_SLEEVE["max_weight"],
        }
    ],
}

SCENARIOS = (
    "equal_weight", "min_volatility", "max_sharpe", "risk_parity", "hrp", "min_cvar",
)

# Scenarios that need no expected-return forecast. Everything here is a pure
# covariance/distribution problem, so it degrades gracefully as the estimate of
# mu gets worse -- which, on one year of data, it always is.
FORECAST_FREE_SCENARIOS = ("min_volatility", "risk_parity", "hrp", "min_cvar", "equal_weight")

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


class MixedUnitUniverseBlocked(Exception):
    """F1 is resolved (TSE_PRICE_UNIT = "rial", storage-wide), so this never
    raises in current operation. Kept as a fail-safe: if TSE_PRICE_UNIT is ever
    reverted to "unverified" (see docs/F1_POLICY.md), mixed
    TSE/non-TSE universes fail closed again instead of silently optimizing
    across a possible 10x unit mismatch.
    """

    def __init__(self, tse_keys: list[str], other_keys: list[str]):
        super().__init__(
            "TSE price unit is unverified; cannot mix TSE equities with "
            "non-TSE assets in optimization until F1 is resolved."
        )
        self.tse_keys = list(tse_keys)
        self.other_keys = list(other_keys)


def _guard_mixed_tse_units(keys) -> None:
    from marketdata.currency import partition_tse_asset_keys, tse_unit_verified

    if tse_unit_verified():
        return
    tse_keys, other_keys = partition_tse_asset_keys(keys)
    if tse_keys and other_keys:
        raise MixedUnitUniverseBlocked(tse_keys, other_keys)


# ---------- helpers ----------------------------------------------------------


def _asset_class_map(universe: list[str] | None = None) -> dict[str, str]:
    """{asset_key: asset_class} for the same universe the returns df uses."""
    return asset_class_map(universe)


def _resolve_sleeves(
    constraints: dict,
    class_map: dict[str, str],
    columns: list[str],
) -> list[dict]:
    """Expand sleeve class lists into the keys present in this universe."""
    out = []
    for raw in constraints.get("sleeves") or []:
        classes = [c for c in (raw.get("classes") or [])]
        keys = [k for k in columns if class_map.get(k) in classes]
        if not keys:
            continue
        out.append({
            "id": raw.get("id", "sleeve"),
            "label": raw.get("label") or HARD_ASSET_SLEEVE.get("label", ""),
            "classes": classes,
            "assets": keys,
            "max_combined_weight": float(raw.get("max_weight", 1.0)),
        })
    return out


def _shrunk_covariance(returns: pd.DataFrame) -> pd.DataFrame:
    """Ledoit-Wolf shrunk covariance of daily returns (DataFrame, daily scale)."""
    complete = returns.dropna(how="any")
    if complete.empty:
        raise UniverseTooSmall([])
    mat = complete.to_numpy()
    lw = LedoitWolf().fit(mat)
    return pd.DataFrame(lw.covariance_, index=returns.columns, columns=returns.columns)


def _correlation_clusters(returns: pd.DataFrame, threshold: float) -> list[list[str]]:
    """Group assets whose pairwise return correlation is >= `threshold`.

    COMPLETE linkage, not single linkage. The previous union-find was transitive:
    rho(A,B) >= t and rho(B,C) >= t merged A, B and C even when rho(A,C) was ~0.
    On a rial-denominated book almost every pair clears the bar through the
    shared devaluation factor, so that collapsed the whole universe into one
    cluster capped at 50% -- infeasible at full investment by construction, which
    surfaced to the user as an opaque SolverError.

    A member now joins only if it correlates above the threshold with EVERY
    existing member, so the cap applies to assets that really are one bet.
    Greedy (seeded by the strongest remaining pair) rather than optimal: exact
    complete-linkage clustering is NP-hard and this runs per request.
    """
    complete = returns.dropna(how="any")
    cols = list(complete.columns)
    if len(cols) < 2:
        return [[c] for c in cols]

    corr = complete.corr()

    def rho(a: str, b: str) -> float:
        value = corr.loc[a, b]
        return float(value) if np.isfinite(value) else 0.0

    remaining = list(cols)
    clusters: list[list[str]] = []
    while remaining:
        # Seed with the strongest pair still available; if none clears the bar,
        # every survivor is its own singleton cluster.
        best_pair, best_rho = None, threshold
        for i, a in enumerate(remaining):
            for b in remaining[i + 1:]:
                r = rho(a, b)
                if r >= best_rho:
                    best_pair, best_rho = (a, b), r
        if best_pair is None:
            clusters.extend([c] for c in remaining)
            break
        cluster = list(best_pair)
        # Grow only while the NEW member clears the threshold against all members.
        grew = True
        while grew:
            grew = False
            for candidate in remaining:
                if candidate in cluster:
                    continue
                if all(rho(candidate, member) >= threshold for member in cluster):
                    cluster.append(candidate)
                    grew = True
        clusters.append(sorted(cluster))
        remaining = [c for c in remaining if c not in cluster]
    return clusters


def _cluster_summary(
    returns: pd.DataFrame,
    clusters: list[list[str]],
    *,
    cap: float,
) -> list[dict]:
    """Human-readable cluster metadata for the API payload."""
    complete = returns.dropna(how="any")
    corr = complete.corr() if len(complete.columns) >= 2 else None
    out = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        avg_corr = min_corr = None
        if corr is not None:
            pairs = []
            for i, a in enumerate(cluster):
                for b in cluster[i + 1 :]:
                    if a in corr.index and b in corr.columns:
                        pairs.append(float(corr.loc[a, b]))
            if pairs:
                avg_corr = float(np.mean(pairs))
                # Under complete linkage this is the binding number: it is the
                # weakest pair in the group, so it shows the cap was not applied
                # to assets chained together through a third one.
                min_corr = float(np.min(pairs))
        out.append({
            "assets": cluster,
            "avg_pairwise_correlation": avg_corr,
            "min_pairwise_correlation": min_corr,
            "max_combined_weight": cap,
        })
    return out


def _add_ef_constraints(
    ef,
    columns: list[str],
    *,
    class_map: dict[str, str],
    max_weight_per_class: dict[str, float],
    correlation_clusters: list[list[str]] | None,
    max_weight_per_correlation_cluster: float,
    sleeves: list[dict] | None = None,
) -> None:
    for cls, cap in max_weight_per_class.items():
        idx = [i for i, k in enumerate(columns) if class_map.get(k) == cls]
        if idx and cap < 1.0:
            cap_f = float(cap)
            ef.add_constraint(lambda x, idx=idx, cap_f=cap_f: cp.sum(x[idx]) <= cap_f)
    if correlation_clusters and max_weight_per_correlation_cluster < 1.0:
        for cluster in correlation_clusters:
            if len(cluster) < 2:
                continue
            idx = [i for i, k in enumerate(columns) if k in cluster]
            if idx:
                cap_f = float(max_weight_per_correlation_cluster)
                ef.add_constraint(lambda x, idx=idx, cap_f=cap_f: cp.sum(x[idx]) <= cap_f)
    for sleeve in sleeves or []:
        cap = float(sleeve.get("max_combined_weight", 1.0))
        if cap >= 1.0:
            continue
        idx = [i for i, k in enumerate(columns) if k in set(sleeve.get("assets") or [])]
        if idx:
            cap_f = cap
            ef.add_constraint(lambda x, idx=idx, cap_f=cap_f: cp.sum(x[idx]) <= cap_f)


def _enforce_caps(
    weights: dict[str, float],
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
    correlation_clusters: list[list[str]] | None = None,
    max_weight_per_correlation_cluster: float = 1.0,
    sleeves: list[dict] | None = None,
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

        # --- Step 2b: correlated-asset cluster cap ---
        if correlation_clusters and max_weight_per_correlation_cluster < 1.0:
            for cluster in correlation_clusters:
                if len(cluster) < 2:
                    continue
                cluster_keys = [k for k in cluster if k in w]
                total = sum(w[k] for k in cluster_keys)
                cap = float(max_weight_per_correlation_cluster)
                if total > cap + 1e-12 and total > 0:
                    factor = cap / total
                    for k in cluster_keys:
                        new_v = w[k] * factor
                        slack += w[k] - new_v
                        w[k] = new_v

        # --- Step 2c: economic sleeve cap (Gold + Cash even when ρ is low) ---
        active_sleeves = [
            s for s in (sleeves or [])
            if float(s.get("max_combined_weight", 1.0)) < 1.0
        ]
        for sleeve in active_sleeves:
            sleeve_keys = [k for k in (sleeve.get("assets") or []) if k in w]
            total = sum(w[k] for k in sleeve_keys)
            cap = float(sleeve["max_combined_weight"])
            if total > cap + 1e-12 and total > 0:
                factor = cap / total
                for k in sleeve_keys:
                    new_v = w[k] * factor
                    slack += w[k] - new_v
                    w[k] = new_v

        # --- Step 3: redistribute slack to assets with headroom ---
        if slack <= 1e-12:
            break
        # Headroom per asset: min(asset cap - current, class cap - class total).
        cls_total = {cls: sum(w[k] for k in ks) for cls, ks in by_class.items()}
        cluster_key = {
            k: tuple(sorted(cluster))
            for cluster in (correlation_clusters or [])
            if len(cluster) >= 2
            for k in cluster
        }
        cluster_total = {
            cid: sum(w.get(k, 0.0) for k in keys if cluster_key.get(k) == cid)
            for cid in set(cluster_key.values())
        }
        sleeve_key = {
            k: sleeve.get("id", "sleeve")
            for sleeve in active_sleeves
            for k in (sleeve.get("assets") or [])
        }
        sleeve_cap = {
            sleeve.get("id", "sleeve"): float(sleeve["max_combined_weight"])
            for sleeve in active_sleeves
        }
        sleeve_total = {
            sid: sum(w.get(k, 0.0) for k in keys if sleeve_key.get(k) == sid)
            for sid in set(sleeve_key.values())
        }
        headroom = {}
        for k in keys:
            cls = class_map.get(k, "Other")
            hr_asset = max_weight_per_asset - w[k]
            hr_class = caps_class[k] - cls_total[cls]
            hr_cluster = 1.0
            cid = cluster_key.get(k)
            if cid is not None:
                hr_cluster = max(
                    0.0,
                    float(max_weight_per_correlation_cluster) - cluster_total.get(cid, 0.0),
                )
            hr_sleeve = 1.0
            sid = sleeve_key.get(k)
            if sid is not None:
                hr_sleeve = max(0.0, sleeve_cap.get(sid, 1.0) - sleeve_total.get(sid, 0.0))
            headroom[k] = max(0.0, min(hr_asset, hr_class, hr_cluster, hr_sleeve))
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
    """Annualized (return, volatility, Sharpe) for a weight dict over mu/cov.

    `weight_covered` is the share of the requested weight that mu/cov could
    actually price. The metrics describe THAT share renormalized to 1.0, so a
    Sharpe computed over 70% of a book is never mistaken for the whole book's --
    the same disclosure convention `_portfolio_returns` uses in diagnostics.
    """
    cols = [k for k in weights if k in mu.index]
    empty = {
        "expected_return_annual": 0.0, "annualized_volatility": 0.0,
        "sharpe": 0.0, "weight_covered": 0.0,
    }
    if not cols:
        return empty
    requested = sum(float(v) for v in weights.values())
    w = np.array([weights[k] for k in cols], dtype=float)
    if w.sum() <= 0:
        return empty
    covered = float(w.sum()) / requested if requested > 0 else 0.0
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
        "weight_covered": _finite(covered),
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
    return hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


# ---------- held-book adjustments -------------------------------------------
#
# Everything below applies ONLY when the caller passed `held_keys` -- i.e. the
# universe is the user's own portfolio ("Optimal version of my portfolio"), not
# the market screen that Best Overall runs. The market path must stay on the
# published diversification policy, so each of these is gated at the call site.


def _proxy_groups(keys) -> dict[str, list[str]]:
    """{proxy_key: [member_keys]} for held assets that borrow another's series.

    A Swiss gold bar has no provider symbol; `Asset.proxy_key` points it at
    `gold_18k_gram` so its RISK is measured from real gold history instead of the
    handful of manual valuation marks it produced (see returns.resolve_universe).

    The optimizer must not receive those as separate columns: they are the same
    series, so the covariance is singular and the split between them arbitrary --
    which is exactly what models.py warns about. Merging them into the proxy's
    single column keeps the estimate well-posed AND keeps the holding in the
    portfolio, instead of dropping it for having no history of its own.
    """
    from portfolio.models import Asset

    groups: dict[str, list[str]] = {}
    rows = Asset.objects.filter(key__in=list(keys)).exclude(proxy_key="").values_list(
        "key", "proxy_key"
    )
    for key, proxy in rows:
        if key == proxy:
            continue
        groups.setdefault(proxy, []).append(key)
    return {proxy: sorted(members) for proxy, members in groups.items()}


def _collapse_onto_proxies(
    weights: dict[str, float], groups: dict[str, list[str]]
) -> dict[str, float]:
    """Sum each proxy group's members into one position on the proxy key."""
    if not groups:
        return dict(weights)
    member_to_proxy = {m: p for p, members in groups.items() for m in members}
    collapsed: dict[str, float] = {}
    for key, weight in weights.items():
        target = member_to_proxy.get(key, key)
        collapsed[target] = collapsed.get(target, 0.0) + float(weight)
    return collapsed


def _expand_from_proxies(
    weights: dict[str, float],
    groups: dict[str, list[str]],
    current_weights: dict[str, float],
) -> dict[str, float]:
    """Split each solved proxy weight back across the members that fed it.

    Pro-rata by current weight within the group: the optimizer had no basis to
    prefer one gold gram over another, so it must not invent one. A group with no
    current weight splits evenly.
    """
    if not groups:
        return dict(weights)
    out: dict[str, float] = {}
    for key, weight in weights.items():
        members = groups.get(key)
        if not members:
            out[key] = out.get(key, 0.0) + float(weight)
            continue
        # The proxy itself may also be held directly alongside its members.
        holders = ([key] if current_weights.get(key) else []) + members
        shares = {h: float(current_weights.get(h, 0.0)) for h in holders}
        total = sum(shares.values())
        if total <= 0:
            for h in holders:
                out[h] = out.get(h, 0.0) + float(weight) / len(holders)
        else:
            for h in holders:
                out[h] = out.get(h, 0.0) + float(weight) * shares[h] / total
    return {k: v for k, v in out.items() if v > 1e-9}


def _apply_frozen_sleeve(
    target: dict[str, float],
    current_weights: dict[str, float],
    optimizable_keys: set[str],
) -> tuple[dict[str, float], dict[str, float], float]:
    """Scale the solved target onto the share of the book the solver could see.

    A held asset the solver had to drop is a MEASUREMENT failure -- no history,
    an integrity-gate verdict, or a shared-window drop -- not an investment
    verdict. Leaving it at target 0 made `_rebalance_trades` emit a full SELL for
    it, so the page told the user to liquidate a holding purely because we could
    not price its risk. It keeps its current weight instead, and the optimizer
    allocates only what is left, so the target still spans the whole portfolio.

    Returns `(target, frozen, solved_share)`.
    """
    frozen = {
        k: float(w) for k, w in current_weights.items()
        if k not in optimizable_keys and float(w) > 0
    }
    solved_share = max(1.0 - sum(frozen.values()), 0.0)
    merged = {k: float(v) * solved_share for k, v in target.items()}
    for key, weight in frozen.items():
        merged[key] = merged.get(key, 0.0) + weight
    return merged, frozen, solved_share


def _floor_constraints_for_book(
    constraints: dict,
    current_weights: dict[str, float],
    class_map: dict[str, str],
    columns: list[str],
    correlation_clusters: list[list[str]],
) -> list[dict]:
    """Raise any cap the user's OWN portfolio already breaches (mutates `constraints`).

    DEFAULT_CONSTRAINTS is market-universe policy: 40% per asset, Gold+Cash 50%
    combined, per-class caps. A gold-heavy Iranian book breaches the hard-asset
    sleeve on day one, which made the problem infeasible at full investment, which
    raised SolverError, which the page rendered as "insufficient history" -- a
    data-coverage message for what was actually a policy conflict.

    We never tell a user their existing portfolio is impossible. Each cap is
    floored to whatever the book already holds (and the per-asset cap to 1/n so
    equal weight is always reachable). The optimizer can still recommend moving
    BELOW the floor; it just can't be handed an empty feasible set. Every floor
    applied is returned for the payload so the relaxation is never silent.
    """
    floored: list[dict] = []

    def _record(cap: str, policy: float, value: float) -> None:
        floored.append({
            "cap": cap,
            "policy": round(float(policy), 6),
            "floored_to": round(float(value), 6),
            "reason": "current book already exceeds the policy cap",
        })

    n = max(len(columns), 1)
    policy_asset = float(constraints.get("max_weight_per_asset", 1.0))
    # 1/n guarantees the equal-weight portfolio is always in the feasible set.
    needed_asset = max(1.0 / n, max((float(current_weights.get(k, 0.0)) for k in columns), default=0.0))
    if needed_asset > policy_asset:
        constraints["max_weight_per_asset"] = needed_asset
        _record("max_weight_per_asset", policy_asset, needed_asset)

    current_by_class = class_totals(current_weights, class_map)
    class_caps = dict(constraints.get("max_weight_per_class") or {})
    for cls, held in current_by_class.items():
        policy = float(class_caps.get(cls, 1.0))
        if held > policy:
            class_caps[cls] = held
            _record(f"max_weight_per_class.{cls}", policy, held)
    if class_caps:
        constraints["max_weight_per_class"] = class_caps

    sleeves = []
    for raw in constraints.get("sleeves") or []:
        sleeve = dict(raw)
        policy = float(sleeve.get("max_weight", 1.0))
        held = sum(
            float(w) for k, w in current_weights.items()
            if class_map.get(k) in set(sleeve.get("classes") or [])
        )
        if held > policy:
            sleeve["max_weight"] = held
            _record(f"sleeve.{sleeve.get('id', 'sleeve')}", policy, held)
        sleeves.append(sleeve)
    if sleeves:
        constraints["sleeves"] = sleeves

    policy_cluster = float(constraints.get("max_weight_per_correlation_cluster", 1.0))
    held_cluster = max(
        (
            sum(float(current_weights.get(k, 0.0)) for k in cluster)
            for cluster in correlation_clusters if len(cluster) >= 2
        ),
        default=0.0,
    )
    if held_cluster > policy_cluster:
        constraints["max_weight_per_correlation_cluster"] = held_cluster
        _record("max_weight_per_correlation_cluster", policy_cluster, held_cluster)

    return floored


# ---------- scenario solvers -------------------------------------------------


def _equal_weight(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    **_constraints,
) -> dict[str, float]:
    """Unconstrained, fully invested reference portfolio."""
    weight = 1.0 / len(returns.columns)
    return {key: weight for key in returns.columns}


def _max_sharpe(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
    risk_free_annual: float = RISK_FREE_RATE_ANNUAL,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
    degraded: list[str] | None = None,
    **kwargs,
) -> dict[str, float]:
    # `mu_daily` carries the ESTIMATED mean (shrunk / Black-Litterman). Falling
    # back to the raw sample mean here would silently undo the estimator that
    # the caller chose, so the fallback exists only for direct callers that
    # never went through `estimate_mu`.
    mu_daily = kwargs.get("mu_daily")
    mu = (returns.mean() if mu_daily is None else mu_daily) * periods_per_year
    S = cov_daily * periods_per_year

    solvers = ["CLARABEL", "SCS", "OSQP"]
    raw = None
    last_exc = None

    columns = list(S.columns)
    mu = mu.reindex(columns)
    corr_clusters = kwargs.get("correlation_clusters") or []
    max_cluster = float(kwargs.get("max_weight_per_correlation_cluster", 1.0))
    sleeves = kwargs.get("sleeves") or []
    use_sleeve = bool(sleeves)

    def _try_max_sharpe(*, use_class: bool, use_corr: bool, use_sleeve: bool):
        nonlocal last_exc
        for solver in solvers:
            try:
                ef = EfficientFrontier(
                    mu, S, weight_bounds=(0.0, max_weight_per_asset), solver=solver
                )
                _add_ef_constraints(
                    ef,
                    columns,
                    class_map=class_map if use_class else {},
                    max_weight_per_class=max_weight_per_class if use_class else {},
                    correlation_clusters=corr_clusters if use_corr else None,
                    max_weight_per_correlation_cluster=max_cluster if use_corr else 1.0,
                    sleeves=sleeves if use_sleeve else None,
                )
                return ef.max_sharpe(risk_free_rate=risk_free_annual)
            except Exception as e:
                last_exc = e
        return None

    raw = _try_max_sharpe(use_class=True, use_corr=True, use_sleeve=use_sleeve)
    if raw is None:
        raw = _try_max_sharpe(use_class=False, use_corr=True, use_sleeve=use_sleeve)
        if raw is not None and degraded is not None:
            degraded.append("max_sharpe_class_constraints_relaxed")
    if raw is None:
        raw = _try_max_sharpe(use_class=False, use_corr=False, use_sleeve=use_sleeve)
        if raw is not None and degraded is not None:
            degraded.append("max_sharpe_correlation_constraints_relaxed")
    if raw is None and use_sleeve:
        raw = _try_max_sharpe(use_class=False, use_corr=False, use_sleeve=False)
        if raw is not None:
            use_sleeve = False
            if degraded is not None:
                degraded.append("hard_asset_sleeve_relaxed")

    if raw is None:
        raise SolverError(f"All solvers (CLARABEL, SCS, OSQP) failed to solve Max-Sharpe: {last_exc}")

    weights = {k: float(v) for k, v in raw.items() if v > 1e-6}
    # Belt-and-braces: post-hoc cap in case a class constraint was relaxed.
    return _enforce_caps(
        weights,
        max_weight_per_asset=max_weight_per_asset,
        max_weight_per_class=max_weight_per_class,
        class_map=class_map,
        correlation_clusters=corr_clusters,
        max_weight_per_correlation_cluster=max_cluster,
        sleeves=sleeves if use_sleeve else None,
    )


def _min_volatility(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
    degraded: list[str] | None = None,
    **kwargs,
) -> dict[str, float]:
    # min-vol never reads mu -- it is a pure covariance problem, which is why it
    # is the most defensible scenario on this page. EfficientFrontier still
    # requires the argument, so pass whatever the caller estimated.
    mu_daily = kwargs.get("mu_daily")
    mu = (returns.mean() if mu_daily is None else mu_daily) * periods_per_year
    S = cov_daily * periods_per_year

    solvers = ["CLARABEL", "SCS", "OSQP"]
    raw = None
    last_exc = None

    columns = list(S.columns)
    mu = mu.reindex(columns)
    corr_clusters = kwargs.get("correlation_clusters") or []
    max_cluster = float(kwargs.get("max_weight_per_correlation_cluster", 1.0))
    sleeves = kwargs.get("sleeves") or []
    use_sleeve = bool(sleeves)

    def _try_min_vol(*, use_class: bool, use_corr: bool, use_sleeve: bool):
        nonlocal last_exc
        for solver in solvers:
            try:
                ef = EfficientFrontier(
                    mu, S, weight_bounds=(0.0, max_weight_per_asset), solver=solver
                )
                _add_ef_constraints(
                    ef,
                    columns,
                    class_map=class_map if use_class else {},
                    max_weight_per_class=max_weight_per_class if use_class else {},
                    correlation_clusters=corr_clusters if use_corr else None,
                    max_weight_per_correlation_cluster=max_cluster if use_corr else 1.0,
                    sleeves=sleeves if use_sleeve else None,
                )
                return ef.min_volatility()
            except Exception as e:
                last_exc = e
        return None

    raw = _try_min_vol(use_class=True, use_corr=True, use_sleeve=use_sleeve)
    if raw is None:
        raw = _try_min_vol(use_class=False, use_corr=True, use_sleeve=use_sleeve)
        if raw is not None and degraded is not None:
            degraded.append("min_volatility_class_constraints_relaxed")
    if raw is None:
        raw = _try_min_vol(use_class=False, use_corr=False, use_sleeve=use_sleeve)
        if raw is not None and degraded is not None:
            degraded.append("min_volatility_correlation_constraints_relaxed")
    if raw is None and use_sleeve:
        raw = _try_min_vol(use_class=False, use_corr=False, use_sleeve=False)
        if raw is not None:
            use_sleeve = False
            if degraded is not None:
                degraded.append("hard_asset_sleeve_relaxed")

    if raw is None:
        raise SolverError(f"All solvers (CLARABEL, SCS, OSQP) failed to solve Min-Volatility: {last_exc}")

    weights = {k: float(v) for k, v in raw.items() if v > 1e-6}
    return _enforce_caps(
        weights,
        max_weight_per_asset=max_weight_per_asset,
        max_weight_per_class=max_weight_per_class,
        class_map=class_map,
        correlation_clusters=corr_clusters,
        max_weight_per_correlation_cluster=max_cluster,
        sleeves=sleeves if use_sleeve else None,
    )


def _risk_parity(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
    degraded: list[str] | None = None,
    **kwargs,
) -> dict[str, float]:
    """Convex ERC (Spinu 2013): minimize `w' S w - (2/n) * sum(log(w_i))`.

    The unique minimizer (over w > 0, with no other constraints) is the equal-
    risk-contribution portfolio. The log-barrier keeps weights strictly positive
    and the problem is DCP-compliant in cvxpy. Caps enforced post-hoc since the
    barrier formulation doesn't accept linear inequality caps cleanly.
    """
    corr_clusters = kwargs.get("correlation_clusters") or []
    max_cluster = float(kwargs.get("max_weight_per_correlation_cluster", 1.0))
    sleeves = kwargs.get("sleeves") or []
    S = cov_daily.to_numpy()
    n = S.shape[0]
    keys = list(cov_daily.columns)
    # Add a tiny ridge so S is strictly PD (the log barrier requires PD S).
    S = S + np.eye(n) * 1e-10
    w = cp.Variable(n, pos=True)
    objective = cp.Minimize(0.5 * cp.quad_form(w, cp.psd_wrap(S)) - (2.0 / n) * cp.sum(cp.log(w)))
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
        if degraded is not None:
            degraded.append("risk_parity_solver_failed_equal_weight_used")
        equal = {k: 1.0 / n for k in keys}
        return _enforce_caps(
            equal,
            max_weight_per_asset=max_weight_per_asset,
            max_weight_per_class=max_weight_per_class,
            class_map=class_map,
            correlation_clusters=corr_clusters,
            max_weight_per_correlation_cluster=max_cluster,
            sleeves=sleeves,
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
        correlation_clusters=corr_clusters,
        max_weight_per_correlation_cluster=max_cluster,
        sleeves=sleeves,
        )


def _hrp(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
    degraded: list[str] | None = None,
    **kwargs,
) -> dict[str, float]:
    corr_clusters = kwargs.get("correlation_clusters") or []
    max_cluster = float(kwargs.get("max_weight_per_correlation_cluster", 1.0))
    sleeves = kwargs.get("sleeves") or []
    hrp = HRPOpt(returns=returns.dropna(how="any"))
    try:
        raw = hrp.optimize()
    except Exception:
        # Fall back to equal weights; will still be cap-enforced below.
        if degraded is not None:
            degraded.append("hrp_solver_failed_equal_weight_used")
        raw = {k: 1.0 / len(returns.columns) for k in returns.columns}
    weights = {k: float(v) for k, v in raw.items() if v > 1e-6}
    return _enforce_caps(
        weights,
        max_weight_per_asset=max_weight_per_asset,
        max_weight_per_class=max_weight_per_class,
        class_map=class_map,
        correlation_clusters=corr_clusters,
        max_weight_per_correlation_cluster=max_cluster,
        sleeves=sleeves,
        )


def _min_cvar(
    returns: pd.DataFrame,
    cov_daily: pd.DataFrame,
    *,
    max_weight_per_asset: float,
    max_weight_per_class: dict[str, float],
    class_map: dict[str, str],
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
    degraded: list[str] | None = None,
    **kwargs,
) -> dict[str, float]:
    """Minimize Conditional Value at Risk: the average of the worst 5% of days.

    Variance punishes upside and downside equally, which is not how anyone
    actually experiences a portfolio. CVaR targets only the left tail -- "when
    it goes badly, how badly" -- and reads the empirical return distribution
    directly instead of assuming it is normal. That matters here: Iranian asset
    returns have fat tails and jump together in a devaluation, precisely the
    event this is meant to guard against.

    Needs no expected-return forecast beyond the mean already estimated, and the
    caps are applied post-hoc since EfficientCVaR has no class-cap primitive.
    """
    from pypfopt.efficient_frontier import EfficientCVaR

    corr_clusters = kwargs.get("correlation_clusters") or []
    max_cluster = float(kwargs.get("max_weight_per_correlation_cluster", 1.0))
    sleeves = kwargs.get("sleeves") or []
    mu_daily = kwargs.get("mu_daily")
    mu = (returns.mean() if mu_daily is None else mu_daily) * periods_per_year
    complete = returns.dropna(how="any")
    try:
        cvar = EfficientCVaR(
            mu.reindex(complete.columns),
            complete,
            beta=0.95,
            weight_bounds=(0.0, max_weight_per_asset),
        )
        raw = cvar.min_cvar()
    except Exception:
        if degraded is not None:
            degraded.append("min_cvar_solver_failed_equal_weight_used")
        raw = {k: 1.0 / len(complete.columns) for k in complete.columns}
    weights = {k: float(v) for k, v in raw.items() if v > 1e-6}
    return _enforce_caps(
        weights,
        max_weight_per_asset=max_weight_per_asset,
        max_weight_per_class=max_weight_per_class,
        class_map=class_map,
        correlation_clusters=corr_clusters,
        max_weight_per_correlation_cluster=max_cluster,
        sleeves=sleeves,
    )


_SCENARIO_DISPATCH = {
    "equal_weight": _equal_weight,
    "max_sharpe": _max_sharpe,
    "min_volatility": _min_volatility,
    "risk_parity": _risk_parity,
    "hrp": _hrp,
    "min_cvar": _min_cvar,
}


def summarize_optimizer_inputs(
    returns: pd.DataFrame,
    class_map: dict[str, str],
    *,
    cluster_threshold: float = 0.65,
    risk_free_annual: float = RISK_FREE_RATE_ANNUAL,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> dict:
    """One-shot audit of μ / σ / ρ the optimizer would see on this panel."""
    complete = returns.dropna(how="any")
    if complete.empty:
        return {
            "observations": 0,
            "assets": [],
            "gold_cash_pairs": [],
            "cluster_threshold": cluster_threshold,
        }
    mu = complete.mean() * periods_per_year
    vol = complete.std(ddof=0) * np.sqrt(periods_per_year)
    assets = []
    for key in complete.columns:
        m = _finite(mu[key])
        v = _finite(vol[key])
        sharpe = (m - risk_free_annual) / v if v > 0 else 0.0
        flags = []
        if sharpe > SHARPE_CREDIBILITY_CEILING:
            flags.append("sharpe_ceiling")
        if m > EXPECTED_RETURN_CREDIBILITY_CEILING:
            flags.append("return_ceiling")
        assets.append({
            "key": key,
            "asset_class": class_map.get(key, "Other"),
            "expected_return_annual": m,
            "annualized_volatility": v,
            "sharpe": _finite(sharpe),
            "credibility_flags": flags,
        })
    corr = complete.corr()
    gold_cash = [
        k for k in complete.columns
        if class_map.get(k) in HARD_ASSET_SLEEVE["classes"]
    ]
    pairs = []
    for i, a in enumerate(gold_cash):
        for b in gold_cash[i + 1 :]:
            r = _finite(corr.loc[a, b])
            pairs.append({
                "a": a,
                "b": b,
                "correlation": r,
                "would_cluster": r >= cluster_threshold,
            })
    return {
        "observations": len(complete.index),
        "assets": assets,
        "gold_cash_pairs": pairs,
        "cluster_threshold": cluster_threshold,
    }


# ---------- public entry point -----------------------------------------------


def _window_limitations(returns, history_days: int) -> list[str]:
    """Say so when the data does not span the lookback that was asked for.

    An ingest outage truncates every lookback to the same contiguous stretch
    after the hole, so a 1-year and a 10-year request can return identical
    weights. Without this the four windows look like four analyses that happen
    to agree, rather than one analysis shown four times.
    """
    if returns.empty:
        return []
    span_days = (returns.index.max() - returns.index.min()).days
    if span_days >= history_days * 0.8:
        return []
    return [
        f"Requested a {history_days}-day lookback but only {span_days} days of "
        f"contiguous history exist ({len(returns.index)} sessions); longer "
        "lookbacks will return the same result until the gap is backfilled."
    ]


def optimize(
    *,
    scenario: str,
    current_weights: dict[str, float],
    total_value_tomans: Decimal,
    constraints: dict | None = None,
    user=None,
    history_days: int = 180,
    as_of=None,
    universe: list[str] | None = None,
    basis: str = "nominal_toman",
    universe_mode: str = "market",
    min_observations: int = MIN_DAILY_RETURNS,
    held_keys: frozenset[str] = frozenset(),
    expected_return_method: str = "black_litterman",
    mu_options: dict | None = None,
    include_robustness: bool = False,
) -> dict:
    """Run one optimization scenario and return the full payload.

    The payload includes `cached` (bool) so the client can show whether this
    came from the cache. Cache key includes the current portfolio state so
    account-scoped weights and rebalance trades cannot leak across requests.

    `held_keys` marks this as an optimization of the user's OWN book rather than
    a market screen, and switches on three held-book behaviours: proxied
    holdings merge onto one column, policy caps are floored to what the book
    already holds, and assets the solver could not measure are frozen at their
    current weight instead of being zeroed (which read as a SELL). Passing it
    also relaxes the universe-screening gates inside `daily_returns_matrix`,
    whose job is picking optimizer CANDIDATES -- the wrong question to ask about
    something the user already owns. Default empty reproduces the market path
    that Best Overall depends on.
    """
    held_keys = frozenset(held_keys)
    held_book = bool(held_keys)
    resolved = copy.deepcopy(DEFAULT_CONSTRAINTS)
    if constraints:
        for k, v in constraints.items():
            resolved[k] = v
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    basis = normalize_basis(basis)

    from portfolio.services.returns import normalize_as_of, get_universe_by_mode
    as_of_dt = normalize_as_of(as_of)
    import jdatetime
    from django.utils import timezone

    rate_date = as_of_dt or timezone.now()
    jalali_year = jdatetime.date.fromgregorian(date=rate_date.date()).year
    risk_free_annual = settings.RATE_FOR(jalali_year)

    if universe is None:
        account = user.accounts.first() if user is not None else None
        universe = get_universe_by_mode(universe_mode, user=user, account=account)

    _guard_mixed_tse_units(universe or list(current_weights))

    version = _price_version_fingerprint()
    user_id = user.id if user is not None else 0
    portfolio_hash = _constraints_hash({
        "weights": current_weights,
        "total": str(total_value_tomans),
    })

    if universe is None:
        univ_str = "default"
    else:
        sorted_univ = sorted(universe)
        univ_str = hashlib.md5(
            ",".join(sorted_univ).encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:16]

    as_of_str = "latest" if as_of_dt is None else as_of_dt.date().isoformat()

    # `held_keys` decides which columns survive the gates AND whether the caps
    # are floored, so it must version the cache -- otherwise the strict market
    # payload and the held-aware one collide on the same key. `_returns_cache_key`
    # versions on it for the same reason.
    held_str = hashlib.md5(
        ",".join(sorted(held_keys)).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16] if held_keys else "none"

    # `history_days` belongs here: the four lookback windows on the Best
    # Overall page differ by nothing else, so leaving it out served every
    # window whichever one ran first.
    cache_key = (
        f"opt:{user_id}:{portfolio_hash}:{scenario}:as_of:{as_of_str}:univ_mode:{universe_mode}:univ:{univ_str}:basis:{basis}:hist:{history_days}:held:{held_str}:mu:{expected_return_method}:{_constraints_hash(mu_options or {})}:rb:{int(include_robustness)}:v{version}:{_constraints_hash(resolved)}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        payload = dict(cached)
        payload["cached"] = True
        return payload

    returns, excluded = daily_returns_matrix(
        history_days=history_days,
        as_of=as_of_dt,
        universe=universe,
        basis=basis,
        held_keys=held_keys,
    )
    # Read the measured sampling frequency BEFORE any slicing: `.attrs` does not
    # reliably survive the `dropna`/column-selection below, and a gold-and-crypto
    # book quotes 365 days a year, not 252. Annualizing it at 252 overstates
    # volatility by ~20% and changes which portfolio the solver calls optimal.
    frequency = float(returns.attrs.get("periods_per_year", TRADING_DAYS_PER_YEAR))
    matrix_warnings = list(returns.attrs.get("warnings", []))

    # Proxied holdings (a Swiss bar priced off gold) resolve to the SAME series
    # as their proxy once `held_keys` is set. Handing the solver two identical
    # columns makes the covariance singular and the split between them arbitrary,
    # so collapse each group into one position and expand it again after solving.
    proxy_groups = _proxy_groups(set(current_weights) | set(returns.columns)) if held_book else {}
    if proxy_groups:
        members = {m for ms in proxy_groups.values() for m in ms}
        returns = returns[[c for c in returns.columns if c not in members]]
    solver_weights = _collapse_onto_proxies(current_weights, proxy_groups)

    if returns.empty or len(returns.columns) < 3:
        raise UniverseTooSmall(list(returns.columns) if not returns.empty else [])
    # Each optimized covariance uses one real, shared observation window. Rows
    # with a missing return are unavailable, not zero-return days.
    coverage = {key: float(returns[key].notna().mean()) for key in returns.columns}
    eligible = [
        key for key in returns.columns
        if returns[key].notna().sum() >= min_observations
        and returns[key].std(ddof=0) > 0
    ]
    for key in returns.columns:
        if key not in eligible and not any(item.get("key") == key for item in excluded):
            excluded.append({
                "key": key,
                "reason": "insufficient_optimization_observations",
                "observations": int(returns[key].notna().sum()),
                "coverage": coverage[key],
            })
    if len(eligible) < 3:
        raise UniverseTooSmall(eligible)

    # `returns[eligible].dropna(how="any")` intersects observation DATES
    # across every eligible column. Each column can individually clear
    # `min_observations` yet the shared window still collapses once 50-200
    # columns all have to agree on the same day (45 rows for 200 assets in
    # production). Require the shared window to be >= 10x the asset count;
    # if it isn't, iteratively drop the asset with the shallowest individual
    # history -- the one shrinking the shared window the most -- until it is.
    working = list(eligible)
    complete = returns[working].dropna(how="any")
    fallback_applied = False
    fallback_from_n_assets = None
    required_observations = MIN_OBSERVATIONS_PER_ASSET * len(working)
    while len(complete.index) < required_observations and len(working) > 3:
        if not fallback_applied:
            fallback_applied = True
            fallback_from_n_assets = len(working)
        shallowest = min(working, key=lambda k: returns[k].notna().sum())
        excluded.append({
            "key": shallowest,
            "reason": "insufficient_shared_history",
            "observations": int(returns[shallowest].notna().sum()),
        })
        working.remove(shallowest)
        required_observations = MIN_OBSERVATIONS_PER_ASSET * len(working)
        complete = returns[working].dropna(how="any")

    if len(complete.index) < required_observations:
        raise UniverseTooSmall(working)

    eligible = working
    returns = complete

    # Built over the union so it also covers keys that never reached the solver:
    # frozen holdings and proxy members still need a class for the roll-up.
    class_map = _asset_class_map(
        sorted(set(universe or []) | set(current_weights) | set(returns.columns))
    )
    cov_daily = _shrunk_covariance(returns)
    cov_annual = cov_daily * frequency

    # The mean is the noisiest input in the whole pipeline (see
    # services/expected_returns.py for why), so it is estimated, not just
    # averaged. Only the mean-dependent scenarios consume it; min_volatility,
    # risk_parity and hrp never touch mu, which is exactly why they are the
    # more trustworthy answers here.
    from .expected_returns import estimate_mu

    mu, mu_provenance = estimate_mu(
        returns,
        frequency,
        cov_annual,
        method=expected_return_method,
        risk_free_annual=risk_free_annual,
        **(mu_options or {}),
    )
    # The solvers annualize internally from daily inputs, so hand them a daily
    # mean consistent with whatever estimator produced the annual one.
    mu_daily = mu / frequency

    if scenario == "max_sharpe":
        if not (mu > risk_free_annual).any():
            raise NoAssetBeatsRiskFreeRate(
                f"No asset in the eligible universe has an expected annualized return exceeding "
                f"the risk-free rate of {int(risk_free_annual * 100)}%."
            )

    cluster_threshold = float(resolved.get("correlation_cluster_threshold", 0.65))
    correlation_clusters = _correlation_clusters(returns, cluster_threshold)

    # A user's own book is not a market screen: raise any cap it already breaches
    # so the feasible set is never empty. Must run AFTER the clusters are known
    # and BEFORE the caps are read into the solve.
    constraints_floored = (
        _floor_constraints_for_book(
            resolved, solver_weights, class_map, list(returns.columns), correlation_clusters
        )
        if held_book else []
    )

    max_cluster = float(resolved.get("max_weight_per_correlation_cluster", 1.0))
    sleeves = _resolve_sleeves(resolved, class_map, list(returns.columns))

    solver = _SCENARIO_DISPATCH[scenario]
    degraded = []

    def _solve(active_sleeves):
        return solver(
            returns,
            cov_daily,
            max_weight_per_asset=float(resolved["max_weight_per_asset"]),
            max_weight_per_class=resolved["max_weight_per_class"],
            class_map=class_map,
            risk_free_annual=risk_free_annual,
            periods_per_year=frequency,
            mu_daily=mu_daily,
            degraded=degraded,
            correlation_clusters=correlation_clusters,
            max_weight_per_correlation_cluster=max_cluster,
            sleeves=active_sleeves,
        )

    target = _solve(sleeves)
    total = sum(target.values())
    if abs(total - 1.0) > 1e-6 and sleeves:
        degraded.append("hard_asset_sleeve_relaxed")
        sleeves = []
        target = _solve(sleeves)
        total = sum(target.values())
    if abs(total - 1.0) > 1e-6:
        # Say which universe could not be fully invested. After a fallback drop
        # the surviving assets can all belong to capped classes (e.g. gold +
        # crypto alone cap out at 0.80), and a bare "infeasible" gives the
        # caller no way to tell that apart from a solver failure.
        raise SolverError(
            "Constraints are infeasible for a fully invested portfolio: "
            f"{len(returns.columns)} eligible asset(s) reach only {total:.4f} "
            f"of 1.0 under the active per-asset, per-class and per-cluster caps."
        )
    target = {k: float(v) for k, v in target.items() if v > 1e-6}

    # Metrics are scored on the SOLVED (collapsed, un-frozen) weights, because
    # that is the only set mu/cov can price. Scoring the expanded target instead
    # would silently read proxy members and frozen holdings as zero-return.
    metrics = _portfolio_metrics(
        target, mu, cov_annual, risk_free_annual=risk_free_annual
    )
    # The current book on the SAME mu/cov/window, so "Actual vs. optimized" is a
    # like-for-like comparison. The page used to reconstruct this client-side
    # from the Sharpe identity against a differently-built panel.
    current_metrics = _portfolio_metrics(
        solver_weights, mu, cov_annual, risk_free_annual=risk_free_annual
    )

    # Diversification is the part of this that does NOT depend on forecasting
    # returns, so it is the part worth trusting. Reported for both the current
    # book and the target on the same covariance, so the comparison is real.
    from .diversification import diversification_report

    diversification = {
        "current": diversification_report(solver_weights, cov_annual, class_map),
        "target": diversification_report(target, cov_annual, class_map),
    }

    # Opt-in: ~200 re-solves is far too slow for the four-window page load, so
    # only the single scenario the user is looking at pays for it.
    robustness = None
    if include_robustness:
        robustness = resample_weights(
            returns,
            scenario=scenario,
            class_map=class_map,
            constraints=resolved,
            periods_per_year=frequency,
            risk_free_annual=risk_free_annual,
            mu_daily=mu_daily,
        )
        if proxy_groups:
            robustness["weights"] = _expand_from_proxies(
                robustness["weights"], proxy_groups, current_weights
            )

    # Expand proxy groups back to real holdings, then freeze whatever the solver
    # could not measure. Order matters: freezing works on real asset keys.
    target = _expand_from_proxies(target, proxy_groups, current_weights)
    # "Optimizable" is what REACHED the solver, not what came back with weight.
    # Using the surviving weights would freeze any asset the optimizer measured
    # and deliberately exited, silently suppressing a legitimate SELL -- the
    # mirror image of the bug this whole sleeve exists to fix.
    optimizable_keys = set(eligible)
    for proxy, group_members in proxy_groups.items():
        if proxy in optimizable_keys:
            optimizable_keys.update(group_members)
    frozen: dict[str, float] = {}
    optimized_share = 1.0
    if held_book:
        target, frozen, optimized_share = _apply_frozen_sleeve(
            target, current_weights, optimizable_keys
        )
        metrics["weight_covered"] = _finite(optimized_share)
    target = {k: float(v) for k, v in target.items() if v > 1e-6}

    # Why each frozen asset could not be optimized, from the records the returns
    # pipeline already produced -- "held at current weight" with no reason is
    # indistinguishable from a bug.
    reasons = {
        item.get("key"): item.get("reason")
        for item in list(excluded) + matrix_warnings
        if item.get("key")
    }
    frozen_weights = {
        key: {"weight": round(weight, 6), "reason": reasons.get(key, "not_in_eligible_universe")}
        for key, weight in frozen.items()
    }

    trades = _rebalance_trades(current_weights, target, total_value_tomans)

    # Sanity ceiling: never silently display a non-credible result. The
    # numbers are still returned (the frontend decides how to render them),
    # but the payload states plainly when they failed the plausibility check.
    credibility_reasons = []
    if metrics["sharpe"] > SHARPE_CREDIBILITY_CEILING:
        credibility_reasons.append(
            f"sharpe {metrics['sharpe']:.2f} exceeds the credibility ceiling of "
            f"{SHARPE_CREDIBILITY_CEILING}."
        )
    if metrics["expected_return_annual"] > EXPECTED_RETURN_CREDIBILITY_CEILING:
        credibility_reasons.append(
            f"expected_return_annual {metrics['expected_return_annual']:.2f} exceeds the "
            f"credibility ceiling of {EXPECTED_RETURN_CREDIBILITY_CEILING} "
            f"({EXPECTED_RETURN_CREDIBILITY_CEILING:.0%}/yr)."
        )
    credibility = {
        "plausible": not credibility_reasons,
        "reasons": credibility_reasons,
        "thresholds": {
            "sharpe": SHARPE_CREDIBILITY_CEILING,
            "expected_return_annual": EXPECTED_RETURN_CREDIBILITY_CEILING,
        },
    }

    constraints_applied = resolved if scenario != "equal_weight" else {
        "long_only": True,
        "weighting": "equal",
    }
    payload = {
        "scenario": scenario,
        "basis": basis,
        "universe_mode": universe_mode,
        "eligible_assets": list(returns.columns),
        "excluded_assets": excluded,
        "target_weights": target,
        "target_metrics": metrics,
        "current_metrics": current_metrics,
        "diversification": diversification,
        "robustness": robustness,
        "rebalance_trades": trades,
        "current_weights": current_weights,
        "current_class_weights": class_totals(current_weights, class_map),
        "target_class_weights": class_totals(target, class_map),
        "frozen_weights": frozen_weights,
        "optimized_share": _finite(optimized_share),
        "proxy_groups": proxy_groups,
        "constraints_floored": constraints_floored,
        "constraints_applied": constraints_applied,
        "correlation_clusters": _cluster_summary(
            returns,
            correlation_clusters,
            cap=max_cluster,
        ),
        "sleeves": sleeves,
        "data_window": {
            "start": returns.index.min().isoformat(),
            "end": returns.index.max().isoformat(),
        },
        "observations": len(returns.index),
        "n_assets": len(eligible),
        "required_observations": MIN_OBSERVATIONS_PER_ASSET * len(eligible),
        "fallback_applied": fallback_applied,
        **({"fallback_from_n_assets": fallback_from_n_assets} if fallback_applied else {}),
        "credibility": credibility,
        "coverage": {key: coverage[key] for key in returns.columns},
        "risk_free_rate_annual": risk_free_annual,
        "risk_free_rate_source": getattr(settings, "RISK_FREE_RATE_SOURCE", ""),
        "risk_free_rate_jalali_year": jalali_year,
        "periods_per_year": frequency,
        "degraded": degraded,
        "expected_return_method": expected_return_method,
        "expected_return_provenance": mu_provenance,
        # Whether this scenario's answer depends on forecasting returns at all.
        # The forecast-free ones are the trustworthy part of this page.
        "forecast_free": scenario in FORECAST_FREE_SCENARIOS,
        "limitations": [
            "Decision-support scenario; no portfolio is objectively best.",
            "Expected returns are historical estimates, not forecasts.",
            *(
                [
                    "This scenario needs no return forecast -- it is built from "
                    "volatility and correlation only, which are far better "
                    "estimated from this much data than average returns are."
                ] if scenario in FORECAST_FREE_SCENARIOS else [
                    f"This scenario ranks assets by expected return. On "
                    f"{mu_provenance.get('sample_years', 0)} year(s) of data the "
                    f"typical standard error on an annual mean is about "
                    f"{mu_provenance.get('mean_standard_error', 0):.0%}, which is "
                    "wider than the differences it is ranking on. Treat the "
                    "forecast-free scenarios as the more reliable guide."
                ]
            ),
            (
                f"Highly correlated assets (pairwise r ≥ {cluster_threshold:.0%}) share a "
                f"combined weight cap of {max_cluster:.0%} so the optimizer cannot "
                "concentrate in assets that move together."
            ),
            (
                f"Gold and Cash/FX share a combined hard-asset sleeve cap of "
                f"{HARD_ASSET_SLEEVE['max_weight']:.0%} even when daily correlation "
                "is below the cluster threshold."
            ),
            *(
                [
                    f"{len(frozen_weights)} holding(s) could not be measured over this "
                    "window and are held at their current weight; the optimizer "
                    f"allocated the remaining {optimized_share:.0%}."
                ] if frozen_weights else []
            ),
            *(
                [
                    "Some caps were raised to what the portfolio already holds, so a "
                    "book that breaches policy still produces a target instead of an "
                    "infeasible-constraints error."
                ] if constraints_floored else []
            ),
            *_window_limitations(returns, history_days),
        ],
        "price_version": version,
        "cached": False,
    }
    cache.set(cache_key, payload, timeout=_OPT_CACHE_TTL)
    return payload


# ---------- robustness -------------------------------------------------------


def resample_weights(
    returns: pd.DataFrame,
    *,
    scenario: str,
    class_map: dict[str, str],
    constraints: dict,
    periods_per_year: float,
    risk_free_annual: float,
    mu_daily: pd.Series | None = None,
    n_draws: int = 200,
    seed: int = 12345,
) -> dict:
    """Michaud resampling: how much of this allocation is signal, how much noise.

    Draws `n_draws` bootstrap samples of the return panel (sampling ROWS with
    replacement, which preserves the cross-sectional correlation on each day --
    resampling each column independently would destroy the very structure the
    optimizer is built on), re-solves each, and reports the distribution of
    weights rather than one point estimate.

    The band is the deliverable. If gold's 5th-95th percentile spans 10%-70%,
    the point estimate was never meaningful and no one should trade on it. The
    averaged weights are also more stable than any single solve, because the
    optimizer's habit of piling into whichever asset got lucky averages out
    across draws.

    Deterministic `seed`: the same panel must produce the same band, or a user
    refreshing the page sees the allocation move for no reason.
    """
    complete = returns.dropna(how="any")
    columns = list(complete.columns)
    if len(complete.index) < 10 or len(columns) < 2:
        return {"n_draws": 0, "weights": {}, "bands": {}, "converged": 0}

    solver = _SCENARIO_DISPATCH[scenario]
    rng = np.random.default_rng(seed)
    n_rows = len(complete.index)
    draws: list[dict[str, float]] = []

    for _ in range(n_draws):
        rows = rng.integers(0, n_rows, size=n_rows)
        sample = complete.iloc[rows]
        try:
            cov_sample = _shrunk_covariance(sample)
            # Solve UNCONSTRAINED-by-class and cap post-hoc. Passing class caps
            # into the solver makes cvxpy recompile a fresh constrained problem
            # every draw (~0.2s each, so ~40s for 200 draws -- unusable in a
            # request), and on a degenerate draw it burns the whole relaxation
            # ladder before failing. `_enforce_caps` is pure numpy and applies
            # the same limits, which is exactly what risk_parity and hrp already
            # do. The per-asset bound stays in the solver since it is a cheap
            # box constraint.
            weights = solver(
                sample,
                cov_sample,
                max_weight_per_asset=float(constraints["max_weight_per_asset"]),
                max_weight_per_class={},
                class_map={},
                risk_free_annual=risk_free_annual,
                periods_per_year=periods_per_year,
                mu_daily=mu_daily,
                degraded=None,
                correlation_clusters=[],
                max_weight_per_correlation_cluster=1.0,
                sleeves=None,
            )
            weights = _enforce_caps(
                weights,
                max_weight_per_asset=float(constraints["max_weight_per_asset"]),
                max_weight_per_class=constraints["max_weight_per_class"],
                class_map=class_map,
            )
        except Exception:
            # A bootstrap draw can be degenerate (one asset flat throughout).
            # Skipping it is correct; `converged` reports how many survived so a
            # thin sample cannot masquerade as a confident band.
            continue
        total = sum(weights.values())
        if total <= 0:
            continue
        draws.append({k: v / total for k, v in weights.items()})

    if not draws:
        return {"n_draws": n_draws, "weights": {}, "bands": {}, "converged": 0}

    matrix = np.array([[d.get(k, 0.0) for k in columns] for d in draws])
    mean_w = matrix.mean(axis=0)
    p05 = np.percentile(matrix, 5, axis=0)
    p95 = np.percentile(matrix, 95, axis=0)

    return {
        "n_draws": n_draws,
        "converged": len(draws),
        # Averaged across draws: the resampled ("robust") allocation.
        "weights": {
            k: round(float(mean_w[i]), 6)
            for i, k in enumerate(columns) if mean_w[i] > 1e-6
        },
        "bands": {
            k: {
                "p05": round(float(p05[i]), 6),
                "p95": round(float(p95[i]), 6),
                "width": round(float(p95[i] - p05[i]), 6),
            }
            for i, k in enumerate(columns)
        },
        # One number for "should I trust the point estimate at all?": the widest
        # band across assets. Above ~0.4 the allocation is mostly noise.
        "max_band_width": round(float(np.max(p95 - p05)), 6),
    }


# ---------- efficient frontier -----------------------------------------------


def _solve_ef_min_vol(mu, S, cap, risk_free_annual=RISK_FREE_RATE_ANNUAL):
    last_exc = None
    for solver in ["CLARABEL", "SCS", "OSQP"]:
        try:
            ef = EfficientFrontier(mu, S, weight_bounds=(0.0, cap), solver=solver)
            w = ef.min_volatility()
            ret, vol, sharpe = ef.portfolio_performance(
                risk_free_rate=risk_free_annual
            )
            return ef, w, ret, vol
        except Exception as e:
            last_exc = e
            continue
    raise SolverError(f"Failed to solve min volatility: {last_exc}")


def _solve_ef_max_sharpe(mu, S, cap, risk_free_annual=RISK_FREE_RATE_ANNUAL):
    last_exc = None
    for solver in ["CLARABEL", "SCS", "OSQP"]:
        try:
            ef = EfficientFrontier(mu, S, weight_bounds=(0.0, cap), solver=solver)
            w = ef.max_sharpe(risk_free_rate=risk_free_annual)
            ret, vol, sharpe = ef.portfolio_performance(
                risk_free_rate=risk_free_annual
            )
            return ef, w, ret
        except Exception as e:
            last_exc = e
            continue
    raise SolverError(f"Failed to solve max Sharpe: {last_exc}")


def _efficient_frontier(
    n_points: int = 30,
    *,
    history_days: int = 180,
    as_of=None,
    universe: list[str] | None = None,
    basis: str = "nominal_toman",
    held_keys: frozenset[str] = frozenset(),
) -> dict:
    """Sample the efficient frontier + reference points.

    Sweeps target returns from min-vol to max-Sharpe and solves min-vol at each
    level. Returns the frontier (vol ascending) plus the current-portfolio
    point (None here — the view injects the live point) and max_sharpe / min_vol
    points. Reuses the cached returns matrix so it's cheap on a warm cache.

    `universe` is not optional in practice for the My-Optimal page: the caller
    must pass the held universe, or the frontier is drawn over the whole catalog
    while the chart caption promises the user's own assets.
    """
    from portfolio.services.returns import normalize_as_of
    as_of_dt = normalize_as_of(as_of)
    import jdatetime
    from django.utils import timezone

    rate_date = as_of_dt or timezone.now()
    risk_free_annual = settings.RATE_FOR(
        jdatetime.date.fromgregorian(date=rate_date.date()).year
    )

    returns, _ = daily_returns_matrix(
        history_days=history_days,
        as_of=as_of_dt,
        universe=universe,
        basis=basis,
        held_keys=frozenset(held_keys),
    )
    frequency = float(returns.attrs.get("periods_per_year", TRADING_DAYS_PER_YEAR))
    if returns.empty or len(returns.columns) < 2:
        return {"frontier": [], "max_sharpe": None, "min_volatility": None}
    _guard_mixed_tse_units(list(returns.columns))
    eligible = [k for k in returns.columns if returns[k].std(ddof=0) > 0]
    if len(eligible) < 2:
        return {"frontier": [], "max_sharpe": None, "min_volatility": None}
    returns = returns[eligible]
    class_map = _asset_class_map(universe)
    cov_daily = _shrunk_covariance(returns)
    mu = returns.mean() * frequency
    S = cov_daily * frequency

    cap = float(DEFAULT_CONSTRAINTS["max_weight_per_asset"])
    frontier: list[dict] = []
    degraded = []
    try:
        ef_min, w_min, ret_min, vol_min = _solve_ef_min_vol(
            mu, S, cap, risk_free_annual
        )
        ef_max, w_max, ret_max_eff = _solve_ef_max_sharpe(
            mu, S, cap, risk_free_annual
        )
    except Exception:
        return {
            "frontier": [],
            "max_sharpe": None,
            "min_volatility": None,
            "degraded": ["efficient_frontier_reference_solver_failed"],
        }

    target_returns = np.linspace(ret_min, ret_max_eff, n_points)
    seen: set[float] = set()
    for tr in target_returns:
        try:
            solved = False
            for solver in ["CLARABEL", "SCS", "OSQP"]:
                try:
                    ef = EfficientFrontier(mu, S, weight_bounds=(0.0, cap), solver=solver)
                    ef.efficient_return(target_return=float(tr))
                    _, vol, sharpe = ef.portfolio_performance(
                        risk_free_rate=risk_free_annual
                    )
                    solved = True
                    break
                except Exception:
                    continue
            if not solved:
                degraded.append("efficient_frontier_point_solver_failed")
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
            risk_free_annual=risk_free_annual,
            periods_per_year=frequency,
            degraded=degraded,
        )
        ms_metrics = _portfolio_metrics(
            ms, mu, S, risk_free_annual=risk_free_annual
        )
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
            periods_per_year=frequency,
            degraded=degraded,
        )
        mv_metrics = _portfolio_metrics(
            mv, mu, S, risk_free_annual=risk_free_annual
        )
    except Exception:
        mv_metrics = {"expected_return_annual": 0.0, "annualized_volatility": 0.0, "sharpe": 0.0}
        mv = {}

    return {
        "frontier": frontier,
        "max_sharpe": {"weights": ms, "metrics": ms_metrics},
        "min_volatility": {"weights": mv, "metrics": mv_metrics},
        # The view plots the user's current portfolio on this same chart and
        # must annualize it identically, or the point sits on the wrong axis.
        "periods_per_year": frequency,
        "degraded": sorted(set(degraded)),
    }

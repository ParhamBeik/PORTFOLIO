"""How many independent bets a portfolio actually holds.

Diversification is the one risk reduction that costs no expected return, so it
is the part of portfolio construction worth measuring directly rather than
inferring from a Sharpe ratio. Every function here answers the same question
from a different angle: *where is the risk actually coming from, and how much of
it cancels out?*

None of these need a return forecast. That is the point -- they describe the
covariance structure, which is estimable from the data we have, rather than
expected returns, which are not.

Three numbers, in increasing order of usefulness:

  * `effective_holdings`   -- 1/sum(w^2). Counts POSITIONS, ignoring risk. A book
    of ten gold coins scores 10 and is one bet. Reported only as the baseline the
    other two are meant to be compared against.
  * `diversification_ratio`-- weighted-average asset vol / portfolio vol. 1.0
    means nothing cancelled; higher means the assets offset each other.
  * `effective_bets`       -- 1/sum(rc^2) over RISK contributions, not weights.
    This is the honest count: ten perfectly correlated coins score ~1.

`risk_contributions` breaks the portfolio variance down per asset. It sums to
1.0 by Euler's theorem on the (homogeneous, degree-1) volatility function, so
each entry reads directly as "this holding is X% of my risk" -- which is usually
nothing like "this holding is X% of my money", and that gap is the finding.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .classification import class_totals


def _aligned(weights: dict[str, float], cov: pd.DataFrame):
    """(keys, normalized weight vector, covariance submatrix) or None."""
    cols = [k for k in weights if k in cov.index]
    if len(cols) < 1:
        return None
    w = np.array([float(weights[k]) for k in cols], dtype=float)
    if w.sum() <= 0:
        return None
    w = w / w.sum()
    sub = cov.reindex(index=cols, columns=cols).to_numpy()
    if not np.all(np.isfinite(sub)):
        return None
    return cols, w, sub


def risk_contributions(weights: dict[str, float], cov: pd.DataFrame) -> dict[str, float]:
    """Share of portfolio VOLATILITY attributable to each asset (sums to 1.0).

    RC_i = w_i * (Sigma w)_i / (w' Sigma w). Euler's theorem makes these add to
    the total exactly, so they can be read as percentages without a fudge factor.

    This is the single most actionable number on the page: a 10% position in a
    volatile, uncorrelated asset and a 40% position in something that moves with
    everything else can contribute the same risk, and only this decomposition
    shows it.
    """
    aligned = _aligned(weights, cov)
    if aligned is None:
        return {}
    cols, w, sub = aligned
    port_var = float(w @ sub @ w)
    if port_var <= 0:
        return {}
    marginal = sub @ w
    return {
        key: float(w[i] * marginal[i] / port_var)
        for i, key in enumerate(cols)
    }


def effective_bets(weights: dict[str, float], cov: pd.DataFrame) -> float:
    """Inverse Herfindahl of RISK contributions: how many independent bets.

    Ten perfectly correlated gold coins score ~1, because they are one bet.
    Equals the asset count only when every asset contributes risk equally AND
    they are mutually uncorrelated -- the theoretical maximum.
    """
    contributions = risk_contributions(weights, cov)
    if not contributions:
        return 0.0
    squared = sum(v * v for v in contributions.values())
    return float(1.0 / squared) if squared > 0 else 0.0


def effective_holdings(weights: dict[str, float]) -> float:
    """Inverse Herfindahl of WEIGHTS. Counts positions, blind to correlation.

    Included so the UI can show it beside `effective_bets`: the gap between the
    two is exactly the amount of diversification the user thinks they have but
    does not.
    """
    values = [float(v) for v in weights.values() if float(v) > 0]
    total = sum(values)
    if total <= 0:
        return 0.0
    squared = sum((v / total) ** 2 for v in values)
    return float(1.0 / squared) if squared > 0 else 0.0


def diversification_ratio(weights: dict[str, float], cov: pd.DataFrame) -> float:
    """Weighted-average asset volatility / portfolio volatility.

    1.0 means no risk cancelled (everything moves together); 2.0 means the
    portfolio carries half the volatility of its parts. Uses the same shrunk
    covariance the optimizer solved on, so it agrees with the reported vol.
    """
    aligned = _aligned(weights, cov)
    if aligned is None:
        return 1.0
    _cols, w, sub = aligned
    asset_vols = np.sqrt(np.clip(np.diag(sub), 0.0, None))
    port_vol = float(np.sqrt(max(w @ sub @ w, 0.0)))
    if port_vol <= 0:
        return 1.0
    return float(np.sum(w * asset_vols) / port_vol)


def concentration_gap(weights: dict[str, float], cov: pd.DataFrame) -> list[dict]:
    """Assets whose share of RISK materially exceeds their share of money.

    The classic surprise: a 12% crypto sleeve carrying 45% of portfolio risk.
    Sorted by the gap, largest first, so the UI can lead with the worst offender.
    """
    contributions = risk_contributions(weights, cov)
    if not contributions:
        return []
    total_weight = sum(float(v) for v in weights.values()) or 1.0
    rows = []
    for key, rc in contributions.items():
        share = float(weights.get(key, 0.0)) / total_weight
        rows.append({
            "key": key,
            "weight_share": round(share, 6),
            "risk_share": round(rc, 6),
            "gap": round(rc - share, 6),
        })
    rows.sort(key=lambda r: r["gap"], reverse=True)
    return rows


def diversification_report(
    weights: dict[str, float],
    cov: pd.DataFrame,
    class_map: dict[str, str] | None = None,
) -> dict:
    """Every diversification measure for one weight vector, in one payload.

    `cov` must be the ANNUALIZED covariance the optimizer used, so the numbers
    reconcile with the volatility reported beside them.
    """
    contributions = risk_contributions(weights, cov)
    report = {
        "effective_bets": round(effective_bets(weights, cov), 4),
        "effective_holdings": round(effective_holdings(weights), 4),
        "diversification_ratio": round(diversification_ratio(weights, cov), 4),
        "risk_contributions": {k: round(v, 6) for k, v in contributions.items()},
        "concentration_gap": concentration_gap(weights, cov),
        "n_assets": len([v for v in weights.values() if float(v) > 0]),
    }
    if class_map:
        # Risk by asset class answers "am I actually hedged, or do I just own
        # things with different names?" -- the per-class weight split cannot.
        report["risk_by_class"] = class_totals(contributions, class_map)
        report["weight_by_class"] = class_totals(weights, class_map)
    return report


def diversifier_candidates(
    portfolio_series: pd.Series,
    candidate_returns: pd.DataFrame,
    *,
    entry_weight: float = 0.05,
    periods_per_year: float = 252.0,
    min_observations: int = 60,
) -> list[dict]:
    """Rank candidates by how much portfolio volatility each would REMOVE.

    "What should I buy next?" answered as a diversification question rather than
    a past-returns one. Sorting candidates by return picks the things that
    already went up; sorting by this picks the things that do not move with what
    you already own, which is the only risk reduction that costs no expected
    return.

    Adding one candidate at weight `w` gives a two-asset problem against the
    book as a whole:

        sigma_new^2 = (1-w)^2 sigma_p^2 + w^2 sigma_c^2
                      + 2 w (1-w) sigma_p sigma_c rho

    Only `rho` and `sigma_c` vary per candidate, so this is O(n) in the number
    of candidates. Building a (held + candidates) covariance matrix instead
    would be 296x296 estimated from ~365 observations -- singular, and slower to
    get a worse answer.

    `vol_reduction` is positive when the candidate lowers portfolio volatility.
    Returns are reported alongside but never used for ranking; the caller plots
    both so the tradeoff stays visible.
    """
    if portfolio_series is None or portfolio_series.empty or candidate_returns.empty:
        return []
    scale = float(np.sqrt(periods_per_year))
    port_vol_daily = float(portfolio_series.std(ddof=1))
    if not np.isfinite(port_vol_daily) or port_vol_daily <= 0:
        return []

    w = float(entry_weight)
    rows = []
    for key in candidate_returns.columns:
        pair = pd.concat(
            [portfolio_series.rename("_p"), candidate_returns[key].rename("_c")],
            axis=1,
        ).dropna(how="any")
        if len(pair.index) < min_observations:
            continue
        cand_vol_daily = float(pair["_c"].std(ddof=1))
        if not np.isfinite(cand_vol_daily) or cand_vol_daily <= 0:
            continue
        # Recompute the portfolio's own vol on the OVERLAP, not the full series:
        # comparing a candidate measured on 90 shared days against a portfolio
        # measured on 365 would credit it for a quieter stretch it never saw.
        port_vol_overlap = float(pair["_p"].std(ddof=1))
        if not np.isfinite(port_vol_overlap) or port_vol_overlap <= 0:
            continue
        rho = float(pair["_p"].corr(pair["_c"]))
        if not np.isfinite(rho):
            continue
        blended_var = (
            (1 - w) ** 2 * port_vol_overlap**2
            + w**2 * cand_vol_daily**2
            + 2 * w * (1 - w) * port_vol_overlap * cand_vol_daily * rho
        )
        blended_vol = float(np.sqrt(max(blended_var, 0.0)))
        rows.append({
            "key": key,
            "correlation": round(rho, 4),
            "volatility": round(cand_vol_daily * scale, 6),
            "total_return": round(float((1.0 + pair["_c"]).prod() - 1.0), 6),
            "vol_reduction": round((port_vol_overlap - blended_vol) * scale, 6),
            "observations": int(len(pair.index)),
        })
    rows.sort(key=lambda r: r["vol_reduction"], reverse=True)
    return rows

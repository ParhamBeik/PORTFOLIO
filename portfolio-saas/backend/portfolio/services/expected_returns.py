"""Expected-return estimators for the optimizer.

The sample mean is the default in most textbooks and is the weakest link in
practice. Its standard error scales with the calendar SPAN of the sample, not
with how finely you sample it: measuring daily instead of weekly gives you more
rows but no more information about the mean. Over one year, a 40%-volatility
asset's annual mean cannot be pinned down closer than roughly +/-40%. Max-Sharpe
is maximally sensitive to exactly that input, which is why an unconstrained
solve piles into whichever asset happened to run up during the window.

Volatilities and correlations do NOT share this problem -- they converge with
sampling frequency. That asymmetry is the whole argument for preferring
methods that lean on the covariance (min-vol, risk parity, HRP) over methods
that lean on the mean, and for shrinking the mean hard when it must be used.

Three estimators, all returning an ANNUALIZED `pd.Series` indexed by asset key:

  * `sample_mean`      -- the historical average. Kept for comparison/audit.
  * `shrunk`           -- James-Stein/Jorion shrinkage toward the grand mean.
  * `black_litterman`  -- reverse-optimized from an equal-weight prior, with
                          optional absolute views.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

METHODS = ("sample_mean", "shrunk", "black_litterman")
DEFAULT_METHOD = "black_litterman"


def sample_mean(returns: pd.DataFrame, periods_per_year: float) -> pd.Series:
    """Historical arithmetic mean, annualized. High variance, unbiased."""
    return returns.mean() * periods_per_year


def shrunk(
    returns: pd.DataFrame,
    periods_per_year: float,
    cov_annual: pd.DataFrame | None = None,
) -> pd.Series:
    """Jorion (1986) Bayes-Stein shrinkage toward the grand mean.

    Pulls every asset's estimated mean toward the cross-sectional average by an
    intensity the DATA chooses: the noisier the estimates relative to how far
    apart they are, the harder the pull. When the sample is long and the assets
    genuinely differ, shrinkage tends to 0 and this reduces to `sample_mean`.

    The estimator trades a little bias for a large variance reduction, which is
    the right trade when the input is as noisy as an annual return estimate --
    an unbiased number you cannot measure is worth less than a biased one you
    can.
    """
    mu = sample_mean(returns, periods_per_year)
    n_assets = len(mu)
    n_obs = len(returns.index)
    if n_assets < 2 or n_obs <= n_assets + 2:
        return mu
    if cov_annual is None:
        cov_annual = returns.cov() * periods_per_year
    sigma = cov_annual.reindex(index=mu.index, columns=mu.index).to_numpy()
    try:
        inv = np.linalg.pinv(sigma)
    except np.linalg.LinAlgError:
        return mu
    ones = np.ones(n_assets)
    denominator = float(ones @ inv @ ones)
    if denominator <= 0:
        return mu
    # Minimum-variance-portfolio mean: the shrinkage target Jorion derives.
    grand_mean = float(ones @ inv @ mu.to_numpy()) / denominator
    deviation = mu.to_numpy() - grand_mean * ones
    quadratic = float(deviation @ inv @ deviation)
    if quadratic <= 0:
        return pd.Series(np.full(n_assets, grand_mean), index=mu.index)
    # Jorion's lambda; bounded to [0, 1] so an extreme sample cannot invert it.
    intensity = (n_assets + 2) / (n_assets + 2 + n_obs * quadratic)
    intensity = float(min(max(intensity, 0.0), 1.0))
    shrunk_values = (1.0 - intensity) * mu.to_numpy() + intensity * grand_mean
    result = pd.Series(shrunk_values, index=mu.index)
    result.attrs["shrinkage_intensity"] = intensity
    result.attrs["shrinkage_target"] = grand_mean
    return result


def black_litterman(
    returns: pd.DataFrame,
    periods_per_year: float,
    cov_annual: pd.DataFrame,
    *,
    risk_free_annual: float = 0.0,
    risk_aversion: float = 2.5,
    tau: float = 0.05,
    absolute_views: dict[str, float] | None = None,
    view_confidences: dict[str, float] | None = None,
) -> pd.Series:
    """Reverse-optimized prior (equal weight), optionally tilted by views.

    Instead of asking "what did this asset return?", BL asks "what return would
    make a sensible baseline portfolio optimal?" and treats THAT as the prior:
    `pi = delta * Sigma * w_prior`. It is built from the covariance, which we
    can estimate, rather than the mean, which we cannot -- so an asset gets a
    high expected return only by carrying risk in the baseline, never by having
    had a lucky window.

    The prior is equal weight rather than market weight: Iranian assets have no
    usable market-capitalization series, and equal weight is the neutral,
    diversification-friendly baseline. With no views the result IS the prior,
    which is the intended conservative default.
    """
    from pypfopt.black_litterman import BlackLittermanModel

    cols = list(cov_annual.columns)
    if not cols:
        return sample_mean(returns, periods_per_year)

    views = {
        k: float(v) for k, v in (absolute_views or {}).items() if k in cols
    }

    # The equal-weight prior: pi = rf + delta * Sigma * w_eq. Computed directly
    # because pypfopt's BlackLittermanModel REQUIRES a view vector and raises
    # without one -- and with no views the posterior is exactly the prior, so
    # there is nothing for the model to do. Routing the no-view case through it
    # silently fell back to the sample mean, which is the estimator this whole
    # module exists to avoid.
    #
    # `delta * Sigma * w_eq` is a RISK PREMIUM -- an excess return over the
    # risk-free asset -- so the risk-free rate has to be added back to get a
    # total expected return. Omitting it is harmless where rf is ~0 and fatal
    # here: against Iran's ~30% rf every asset scored below cash and max_sharpe
    # refused to solve at all.
    w_equal = np.full(len(cols), 1.0 / len(cols))
    premium = float(risk_aversion) * (cov_annual.to_numpy() @ w_equal)
    prior = pd.Series(float(risk_free_annual) + premium, index=cols)

    if not views:
        prior.attrs["bl_prior"] = "equal"
        prior.attrs["bl_tau"] = tau
        prior.attrs["bl_risk_aversion"] = risk_aversion
        prior.attrs["bl_views"] = {}
        return prior.reindex(returns.columns).dropna()

    confidences = None
    omega = None
    if view_confidences:
        picked = [float(view_confidences.get(k, 0.5)) for k in views]
        if all(0.0 < c < 1.0 for c in picked):
            confidences = picked
            omega = "idzorek"

    try:
        model = BlackLittermanModel(
            cov_annual,
            pi=prior,
            risk_aversion=risk_aversion,
            tau=tau,
            absolute_views=views,
            omega=omega,
            view_confidences=confidences,
        )
        posterior = model.bl_returns()
    except Exception:
        # A degenerate covariance can make the posterior unsolvable. Fall back to
        # the prior, which is still risk-based -- never to the sample mean.
        prior.attrs["bl_prior"] = "equal"
        prior.attrs["bl_views_dropped"] = True
        return prior.reindex(returns.columns).dropna()

    result = pd.Series(posterior).reindex(returns.columns).dropna()
    result.attrs["bl_prior"] = "equal"
    result.attrs["bl_tau"] = tau
    result.attrs["bl_risk_aversion"] = risk_aversion
    result.attrs["bl_views"] = views
    return result


def estimate_mu(
    returns: pd.DataFrame,
    periods_per_year: float,
    cov_annual: pd.DataFrame,
    *,
    method: str = DEFAULT_METHOD,
    risk_free_annual: float = 0.0,
    **kwargs,
) -> tuple[pd.Series, dict]:
    """Dispatch to one estimator. Returns `(mu, provenance)`.

    `provenance` goes into the API payload so the number on screen can always be
    traced to how it was produced -- an expected return with no stated method is
    indistinguishable from a guess.
    """
    if method not in METHODS:
        raise ValueError(f"unknown expected-return method: {method}")

    if method == "sample_mean":
        mu = sample_mean(returns, periods_per_year)
    elif method == "shrunk":
        mu = shrunk(returns, periods_per_year, cov_annual)
    else:
        mu = black_litterman(
            returns, periods_per_year, cov_annual,
            risk_free_annual=risk_free_annual, **kwargs,
        )

    provenance = {
        "method": method,
        "periods_per_year": round(float(periods_per_year), 2),
        "observations": int(len(returns.index)),
        **{k: v for k, v in mu.attrs.items()},
    }
    # The honest error bar: SE of a mean return is sigma/sqrt(years), and years
    # is what we have, not observations. Surfacing it stops anyone reading a
    # point estimate as a forecast.
    years = len(returns.index) / periods_per_year if periods_per_year > 0 else 0.0
    if years > 0:
        vols = np.sqrt(np.clip(np.diag(cov_annual.to_numpy()), 0.0, None))
        provenance["mean_standard_error"] = round(float(np.mean(vols) / np.sqrt(years)), 4)
        provenance["sample_years"] = round(float(years), 2)
    return mu, provenance

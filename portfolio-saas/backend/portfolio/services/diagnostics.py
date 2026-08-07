"""Portfolio diagnostics: risk, return, drawdown, VaR for a current allocation.

These metrics run over the live `daily_returns_matrix` and a user's current
weights. They are deliberately explainable (no ML): the point is trustworthy
numbers the frontend can plot next to the optimization suggestions.

All metrics guard against NaN/inf: an asset with insufficient history still
appears in the user's weights, but the metrics are computed over the eligible
subset (assets whose columns actually exist in the returns df).
"""
from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

from portfolio.services import value_user
from .deflator import normalize_basis
from .returns import daily_returns_matrix

# Iran TSE risk-free proxy (Bahar Azadi bond yield ~30%). Annualized.
from django.conf import settings
RISK_FREE_RATE_ANNUAL = float(getattr(settings, "RISK_FREE_RATE_ANNUAL", 0.30))
TRADING_DAYS_PER_YEAR = 252


def _finite(value, default: float = 0.0) -> float:
    """Return value if finite, else default (keeps payloads JSON-safe)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if np.isfinite(f) else default


def _user_real_estate_value(user) -> Decimal:
    """Sum of liquid-equivalent real-estate (house) value across the user's holdings.

    Computed from the live valuation so it reflects the current house formula,
    not a snapshot.
    """
    valuation = value_user(user)
    total = Decimal("0")
    for acct in valuation["accounts"]:
        for item in acct["items"]:
            if item["class"] == "Real Estate":
                total += item["value"]
    return total


def _portfolio_returns(returns: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """Daily return series of the weighted portfolio over the eligible columns."""
    cols = [k for k in weights if k in returns.columns]
    if not cols:
        return pd.Series(dtype=float)
    w = np.array([weights[k] for k in cols], dtype=float)
    w = w / w.sum() if w.sum() else w
    sub = returns[cols].dropna(how="any")
    return pd.Series(sub.to_numpy() @ w, index=sub.index)


def _annualized_volatility(port_series: pd.Series) -> float:
    if port_series.empty:
        return 0.0
    return _finite(np.std(port_series, ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))


def _sharpe(
    port_series: pd.Series,
    risk_free_annual: float = RISK_FREE_RATE_ANNUAL,
) -> float:
    if port_series.empty:
        return 0.0
    ann_return = _finite(np.mean(port_series) * TRADING_DAYS_PER_YEAR)
    ann_vol = _annualized_volatility(port_series)
    if ann_vol == 0:
        return 0.0
    rf_daily = risk_free_annual / TRADING_DAYS_PER_YEAR
    excess = np.mean(port_series) - rf_daily
    return _finite(excess / np.std(port_series, ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))


def _sortino(
    port_series: pd.Series,
    risk_free_annual: float = RISK_FREE_RATE_ANNUAL,
) -> float:
    """Sortino with downside deviation (returns < MAR=rf_daily)."""
    if port_series.empty:
        return 0.0
    rf_daily = risk_free_annual / TRADING_DAYS_PER_YEAR
    excess = port_series - rf_daily
    downside = excess.clip(upper=0.0)
    dd = np.sqrt(np.mean(downside ** 2))
    if dd == 0:
        return 0.0
    return _finite(np.mean(excess) / dd * np.sqrt(TRADING_DAYS_PER_YEAR))


def _max_drawdown(port_series: pd.Series) -> tuple[float, int]:
    """(max drawdown fraction [negative], days under water) for the series."""
    if port_series.empty:
        return 0.0, 0
    wealth = (1.0 + port_series).cumprod()
    running_max = wealth.cummax()
    drawdown = (wealth - running_max) / running_max.replace(0, np.nan)
    mdd = _finite(drawdown.min())
    # Days under water: longest consecutive stretch below the running max.
    under = (wealth < running_max).to_numpy()
    longest = current = 0
    for flag in under:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return mdd, int(longest)


def _rolling_metrics(
    port_series: pd.Series,
    risk_free_annual: float,
    window: int = 30,
) -> list[dict]:
    if len(port_series) < window:
        return []
    volatility = port_series.rolling(window).std(ddof=1) * np.sqrt(
        TRADING_DAYS_PER_YEAR
    )
    excess = port_series - risk_free_annual / TRADING_DAYS_PER_YEAR
    sharpe = (
        excess.rolling(window).mean()
        / port_series.rolling(window).std(ddof=1)
        * np.sqrt(TRADING_DAYS_PER_YEAR)
    )
    drawdown = port_series.rolling(window).apply(
        lambda values: (
            (1 + pd.Series(values)).cumprod()
            / (1 + pd.Series(values)).cumprod().cummax()
            - 1
        ).min(),
        raw=False,
    )
    rows = []
    for date in port_series.index:
        if pd.isna(volatility.get(date)):
            continue
        rows.append({
            "date": date.isoformat(),
            "sharpe": _finite(sharpe.get(date)),
            "volatility": _finite(volatility.get(date)),
            "drawdown": _finite(drawdown.get(date)),
        })
    return rows


def _period_returns(port_series: pd.Series) -> dict:
    """Cumulative simple return over trailing 7d/30d/90d/1y, plus Jalali YTD."""
    if port_series.empty:
        return {}
    import jdatetime

    wealth = (1.0 + port_series).cumprod()
    last_date = port_series.index[-1]
    result = {}
    for label, days in (("7d", 7), ("30d", 30), ("90d", 90), ("1y", 365)):
        window = wealth[wealth.index >= last_date - pd.Timedelta(days=days)]
        if len(window) >= 2:
            result[label] = _finite(window.iloc[-1] / window.iloc[0] - 1.0)

    jalali_last = jdatetime.date.fromgregorian(date=last_date.date())
    greg_new_year = jdatetime.date(jalali_last.year, 1, 1).togregorian()
    ytd_window = wealth[wealth.index.date >= greg_new_year]
    if len(ytd_window) >= 2:
        result["ytd"] = _finite(ytd_window.iloc[-1] / ytd_window.iloc[0] - 1.0)
    return result


def _current_drawdown(port_series: pd.Series) -> float:
    """Drawdown as of the most recent observation (not the historical max)."""
    if port_series.empty:
        return 0.0
    wealth = (1.0 + port_series).cumprod()
    running_max = wealth.cummax()
    drawdown = (wealth - running_max) / running_max.replace(0, np.nan)
    return _finite(drawdown.iloc[-1])


def _concentration_hhi(weights: dict[str, float]) -> float:
    """Herfindahl-Hirschman Index of the weights: sum(w_i^2), normalized to sum=1."""
    total = sum(weights.values()) if weights else 0.0
    if total <= 0:
        return 0.0
    return _finite(sum((w / total) ** 2 for w in weights.values()))


def _best_worst_day(port_series: pd.Series) -> tuple[float, float]:
    if port_series.empty:
        return 0.0, 0.0
    return _finite(port_series.max()), _finite(port_series.min())


def _calmar(port_series: pd.Series) -> float:
    if port_series.empty:
        return 0.0
    mdd, _ = _max_drawdown(port_series)
    if mdd == 0:
        return 0.0
    ann_return = _finite(np.mean(port_series) * TRADING_DAYS_PER_YEAR)
    return _finite(ann_return / abs(mdd))


def _historical_var_cvar(port_series: pd.Series, alpha: float = 0.95) -> tuple[float, float]:
    """Historical VaR and CVaR at the (1-alpha) tail. Returns are losses (negative)."""
    if port_series.empty:
        return 0.0, 0.0
    arr = port_series.dropna().to_numpy()
    if arr.size == 0:
        return 0.0, 0.0
    var = _finite(np.quantile(arr, 1 - alpha))  # negative number for a loss
    tail = arr[arr <= var]
    cvar = _finite(np.mean(tail)) if tail.size else var
    return var, cvar


def _diversification_ratio(
    returns: pd.DataFrame, weights: dict[str, float]
) -> float:
    """Weighted avg asset vol / portfolio vol, using shrunk annualized covariance."""
    cols = [k for k in weights if k in returns.columns]
    if len(cols) < 2:
        return 1.0
    w = np.array([weights[k] for k in cols], dtype=float)
    if w.sum() <= 0:
        return 1.0
    w = w / w.sum()
    sub = returns[cols].dropna(how="any")
    if sub.empty:
        return 1.0
    try:
        lw = LedoitWolf().fit(sub.to_numpy())
        cov = lw.covariance_ * TRADING_DAYS_PER_YEAR
    except Exception:
        return 1.0
    asset_vols = np.sqrt(np.diag(cov))
    weighted_avg_vol = float(np.sum(w * asset_vols))
    port_var = float(w @ cov @ w)
    port_vol = np.sqrt(max(port_var, 0.0))
    if port_vol == 0:
        return 1.0
    return _finite(weighted_avg_vol / port_vol)


def _load_index_returns(target_index: pd.Index, as_of: dt.datetime | None = None) -> pd.Series | None:
    """Load index return series from MarketIndexData, aligned with target_index."""
    from django.conf import settings
    if not getattr(settings, "HISTORICAL_BENCHMARK_ENABLED", False):
        return None
    from marketdata.models import MarketIndexData
    import jdatetime
    import datetime as dt
    from portfolio.services.returns import to_jalali_str

    qs = MarketIndexData.objects.order_by("date", "time")
    if as_of is not None:
        as_of_jalali = to_jalali_str(as_of)
        qs = qs.filter(date__lte=as_of_jalali)

    if not qs.exists():
        return None

    records: dict[dt.date, float] = {}
    for row in qs:
        try:
            parts = [int(p) for p in row.date.split("-")]
            greg_date = jdatetime.date(parts[0], parts[1], parts[2]).togregorian()
            records[greg_date] = float(row.index_overall)
        except Exception:
            continue

    if len(records) < 2:
        return None

    s = pd.Series(records).sort_index()
    s_returns = s.pct_change(fill_method=None)
    s_returns.index = pd.to_datetime(s_returns.index, utc=True).normalize()
    return s_returns.reindex(target_index)


def portfolio_diagnostics(
    current_weights: dict[str, float],
    total_value_tomans: Decimal,
    *,
    user=None,
    history_days: int = 180,
    as_of=None,
    universe: list[str] | None = None,
    basis: str = "nominal_toman"
) -> dict:
    """Compute all diagnostics for the current portfolio.

    `current_weights` is liquid weights only (real estate excluded upstream).
    `user` (optional) is used to compute the real-estate block via the live
    valuation — passed by the view; tests can omit it.
    """
    from portfolio.services.returns import normalize_as_of
    as_of_dt = normalize_as_of(as_of)
    basis = normalize_basis(basis)
    import jdatetime
    from django.utils import timezone

    rate_date = as_of_dt or timezone.now()
    rate_year = jdatetime.date.fromgregorian(date=rate_date.date()).year
    risk_free_annual = settings.RATE_FOR(rate_year)

    returns, excluded = daily_returns_matrix(
        history_days=history_days,
        as_of=as_of_dt,
        universe=universe,
        basis=basis
    )
    port_series = _portfolio_returns(returns, current_weights) if not returns.empty else pd.Series(dtype=float)

    ann_vol = _annualized_volatility(port_series)
    sharpe = _sharpe(port_series, risk_free_annual)
    sortino = _sortino(port_series, risk_free_annual)
    mdd, days_under = _max_drawdown(port_series)
    calmar = _calmar(port_series)
    var95, cvar95 = _historical_var_cvar(port_series, alpha=0.95)
    div_ratio = _diversification_ratio(returns, current_weights) if not returns.empty else 1.0
    period_returns = _period_returns(port_series)
    current_dd = _current_drawdown(port_series)
    hhi = _concentration_hhi(current_weights)
    best_day, worst_day = _best_worst_day(port_series)

    eligible_assets = list(returns.columns) if not returns.empty else []

    # Load index returns and compute benchmark metrics if index history exists
    benchmark_metrics = {}
    if not returns.empty and not port_series.empty:
        index_returns = _load_index_returns(returns.index, as_of=as_of_dt)
        if index_returns is not None:
            aligned = pd.concat(
                [port_series.rename("portfolio"), index_returns.rename("benchmark")],
                axis=1,
            ).dropna(how="any")
            if len(aligned.index) < 2:
                index_returns = None
            else:
                port_for_benchmark = aligned["portfolio"]
                index_returns = aligned["benchmark"]
        if index_returns is not None:
            beta = 0.0
            cov_matrix = np.cov(port_for_benchmark, index_returns)
            benchmark_var = np.var(index_returns, ddof=1)
            if benchmark_var > 0:
                beta = cov_matrix[0][1] / benchmark_var
            
            ann_port_return = np.mean(port_for_benchmark) * TRADING_DAYS_PER_YEAR
            ann_index_return = np.mean(index_returns) * TRADING_DAYS_PER_YEAR
            alpha = ann_port_return - (
                risk_free_annual
                + beta * (ann_index_return - risk_free_annual)
            )
            
            active_returns = port_for_benchmark - index_returns
            tracking_error = np.std(active_returns, ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)
            
            info_ratio = 0.0
            if tracking_error > 0:
                info_ratio = (np.mean(active_returns) * TRADING_DAYS_PER_YEAR) / tracking_error
                
            benchmark_metrics = {
                "beta": _finite(beta),
                "alpha": _finite(alpha),
                "tracking_error": _finite(tracking_error),
                "information_ratio": _finite(info_ratio),
            }

    real_estate: dict = {"value_tomans": "0", "share_of_total": 0.0}
    if user is not None:
        re_value = _user_real_estate_value(user)
        total_decimal = Decimal(str(total_value_tomans)) + re_value
        share = float(re_value / total_decimal) if total_decimal > 0 else 0.0
        real_estate = {
            "value_tomans": str(re_value),
            "share_of_total": _finite(share),
        }

    return {
        "basis": basis,
        "risk_free_rate_annual": risk_free_annual,
        "risk_free_rate_jalali_year": rate_year,
        "analysis_type": "hypothetical_fixed_weight_exposure",
        "current_weights": current_weights,
        "total_value_tomans": str(total_value_tomans),
        "eligible_assets": eligible_assets,
        "excluded_assets": excluded,
        "data_window": {
            "start": returns.index.min().isoformat() if not returns.empty else None,
            "end": returns.index.max().isoformat() if not returns.empty else None,
            "observations": len(port_series.index),
        },
        "metrics": {
            "annualized_volatility": _finite(ann_vol),
            "sharpe": _finite(sharpe),
            "sortino": _finite(sortino),
            "max_drawdown": _finite(mdd),
            "days_under_water": int(days_under),
            "calmar": _finite(calmar),
            "historical_var_95": _finite(var95),
            "historical_cvar_95": _finite(cvar95),
            "diversification_ratio": max(_finite(div_ratio), 1.0),
            "current_drawdown": _finite(current_dd),
            "concentration_hhi": _finite(hhi),
            "best_day": _finite(best_day),
            "worst_day": _finite(worst_day),
            **benchmark_metrics,
        },
        "period_returns": period_returns,
        "rolling": _rolling_metrics(port_series, risk_free_annual),
        "real_estate": real_estate,
    }

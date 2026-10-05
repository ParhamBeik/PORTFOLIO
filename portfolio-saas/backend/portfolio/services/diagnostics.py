"""Portfolio diagnostics: risk, return, drawdown, VaR for a current allocation.

These metrics run over the live `daily_returns_matrix` and a user's current
weights. They are deliberately explainable (no ML): the point is trustworthy
numbers the frontend can plot next to the optimization suggestions.

All metrics guard against NaN/inf: an asset with insufficient history still
appears in the user's weights, but the metrics are computed over the eligible
subset (assets whose columns actually exist in the returns df).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

from portfolio.services import value_user
from . import diversification
from .deflator import normalize_basis
from .returns import TRADING_DAYS_PER_YEAR, correlation_matrix, daily_returns_matrix

# Iran TSE risk-free proxy (Bahar Azadi bond yield ~30%). Annualized.
from django.conf import settings
RISK_FREE_RATE_ANNUAL = float(getattr(settings, "RISK_FREE_RATE_ANNUAL", 0.30))


def _finite(value, default: float = 0.0) -> float:
    """Return value if finite, else default (keeps payloads JSON-safe)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if np.isfinite(f) else default


def _rf_daily(
    risk_free_annual: float, periods_per_year: float = TRADING_DAYS_PER_YEAR
) -> float:
    """Annual risk-free rate -> per-period, geometric convention (matches the
    return side, which compounds via (1+r).cumprod()/.prod()). Simple
    division (rf/252) understates the daily rate at high rf (~13% error
    at rf=0.30).

    `periods_per_year` must be the panel's measured frequency, not a constant:
    a gold panel quotes 365 times a year and a TSE panel ~252, and using the
    wrong one biases every excess-return metric built on this.
    """
    return (1.0 + risk_free_annual) ** (1.0 / periods_per_year) - 1.0


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
    """Daily return series of the weighted portfolio, weighted per day.

    The previous implementation inner-joined the weighted columns with
    dropna(how="any") and then dropped the shallowest-history asset until the
    shared window cleared a 10x-asset-count floor. On a 90-day window (~60
    sessions) that discarded assets by construction for any book of more than
    ~5 holdings, and then renormalized the survivors to 100% -- reporting the
    risk of a fraction of the portfolio under the portfolio's name.

    Instead each day is weighted over whichever assets have an observation that
    day, renormalized by the weight actually covered. No asset is dropped and no
    history is thrown away because some *other* asset was missing that morning.
    `.attrs["mean_weight_covered"]` reports the average share of the requested
    weight the series actually spans, so a thin day stays visible rather than
    being laundered into a confident number.
    """
    cols = [k for k in weights if k in returns.columns]
    if not cols:
        empty = pd.Series(dtype=float)
        empty.attrs.update(
            weights_used={}, weights_rescaled=False, dropped_assets=[],
            mean_weight_covered=0.0,
        )
        return empty

    sub = returns[cols]
    w = pd.Series({k: float(weights[k]) for k in cols}, dtype=float)
    covered = sub.notna().mul(w, axis=1).sum(axis=1)
    series = (
        sub.mul(w, axis=1).sum(axis=1).div(covered.replace(0.0, np.nan)).dropna()
    )
    total_weight = float(w.sum())
    series.attrs.update(
        weights_used={k: float(v / total_weight) if total_weight else 0.0
                      for k, v in w.items()},
        weights_rescaled=False,
        dropped_assets=[],
        mean_weight_covered=_finite(
            covered.reindex(series.index).mean() / total_weight
            if total_weight else 0.0
        ),
    )
    return series


def _annualized_volatility(
    port_series: pd.Series, periods_per_year: float = TRADING_DAYS_PER_YEAR
) -> float:
    if port_series.empty:
        return 0.0
    return _finite(np.std(port_series, ddof=1) * np.sqrt(periods_per_year))


def _sharpe(
    port_series: pd.Series,
    risk_free_annual: float = RISK_FREE_RATE_ANNUAL,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> float:
    if port_series.empty:
        return 0.0
    ann_vol = _annualized_volatility(port_series, periods_per_year)
    if ann_vol == 0:
        return 0.0
    rf_daily = _rf_daily(risk_free_annual, periods_per_year)
    excess = np.mean(port_series) - rf_daily
    return _finite(excess / np.std(port_series, ddof=1) * np.sqrt(periods_per_year))


def _sortino(
    port_series: pd.Series,
    risk_free_annual: float = RISK_FREE_RATE_ANNUAL,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> float:
    """Sortino with downside deviation (returns < MAR=rf_daily)."""
    if port_series.empty:
        return 0.0
    rf_daily = _rf_daily(risk_free_annual, periods_per_year)
    excess = port_series - rf_daily
    downside = excess.clip(upper=0.0)
    dd = np.sqrt(np.mean(downside ** 2))
    if dd == 0:
        return 0.0
    return _finite(np.mean(excess) / dd * np.sqrt(periods_per_year))


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
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> list[dict]:
    if len(port_series) < window:
        return []
    volatility = port_series.rolling(window).std(ddof=1) * np.sqrt(periods_per_year)
    excess = port_series - _rf_daily(risk_free_annual, periods_per_year)
    sharpe = (
        excess.rolling(window).mean()
        / port_series.rolling(window).std(ddof=1)
        * np.sqrt(periods_per_year)
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
    # wealth_before[d] = wealth level immediately BEFORE day d's own return
    # (1.0 before the first observation). A window's return must be measured
    # from this anchor, not from wealth[window.index[0]] which already has
    # that first day's return baked in (that was the off-by-one: it silently
    # dropped the first in-window day from every period figure).
    wealth_before = wealth.shift(1).fillna(1.0)
    last_date = port_series.index[-1]
    result = {}
    for label, days in (("7d", 7), ("30d", 30), ("90d", 90), ("1y", 365)):
        window = wealth[wealth.index >= last_date - pd.Timedelta(days=days)]
        if len(window) >= 2:
            anchor = wealth_before.loc[window.index[0]]
            result[label] = _finite(window.iloc[-1] / anchor - 1.0)

    jalali_last = jdatetime.date.fromgregorian(date=last_date.date())
    greg_new_year = jdatetime.date(jalali_last.year, 1, 1).togregorian()
    ytd_window = wealth[wealth.index.date >= greg_new_year]
    if len(ytd_window) >= 2:
        anchor = wealth_before.loc[ytd_window.index[0]]
        result["ytd"] = _finite(ytd_window.iloc[-1] / anchor - 1.0)
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


CALMAR_WINDOW_DAYS = 3 * 365  # standard Calmar convention: trailing 36 months


def _calmar(
    port_series: pd.Series, periods_per_year: float = TRADING_DAYS_PER_YEAR
) -> tuple[float | None, int]:
    """Calmar = CAGR / |max drawdown| over the trailing 36-month window.

    Returns (calmar_or_None, window_days_used). With less than 36 months of
    history, returns None instead of silently computing over whatever
    shorter window is available and mislabeling it as Calmar; the second
    element then carries the actual (insufficient) days of history so the
    caller can explain the gap.
    """
    if port_series.empty:
        return None, 0
    last_date = port_series.index[-1]
    first_date = port_series.index[0]
    available_days = (last_date - first_date).days
    if available_days < CALMAR_WINDOW_DAYS:
        return None, available_days
    window = port_series[port_series.index >= last_date - pd.Timedelta(days=CALMAR_WINDOW_DAYS)]
    n = len(window)
    if n < 2:
        return None, available_days
    mdd, _ = _max_drawdown(window)
    if mdd == 0:
        return None, CALMAR_WINDOW_DAYS
    cagr = (1.0 + window).prod() ** (periods_per_year / n) - 1.0
    return _finite(cagr / abs(mdd)), CALMAR_WINDOW_DAYS


def _historical_var_cvar(port_series: pd.Series, alpha: float = 0.95) -> tuple[float, float | None]:
    """Historical VaR and CVaR at the (1-alpha) tail, in DAILY return units.

    Returns are losses (negative). CVaR averages the tail beyond VaR; with
    fewer than 5 tail observations that average is not meaningful (it can be
    1-2 points dressed up as a statistic), so it returns None instead.
    """
    if port_series.empty:
        return 0.0, None
    arr = port_series.dropna().to_numpy()
    if arr.size == 0:
        return 0.0, None
    var = _finite(np.quantile(arr, 1 - alpha))  # negative number for a loss
    tail = arr[arr <= var]
    if tail.size < 5:
        return var, None
    cvar = _finite(np.mean(tail))
    return var, cvar


def annualized_cov(
    returns: pd.DataFrame,
    weights: dict[str, float],
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> pd.DataFrame | None:
    """Shrunk annualized covariance over the weighted assets, or None.

    Ledoit-Wolf shrinkage is the same estimator the optimizer solves on, so
    every diversification number derived from this matrix reconciles with the
    volatility reported beside it instead of coming from a second,
    independently-built panel.
    """
    cols = [key for key in weights if key in returns.columns]
    if len(cols) < 2:
        return None
    sub = returns[cols].dropna(how="any")
    if len(sub.index) < 2:
        return None
    try:
        cov = LedoitWolf().fit(sub.to_numpy()).covariance_ * periods_per_year
    except Exception:
        return None
    return pd.DataFrame(cov, index=cols, columns=cols)


def _diversification_ratio(
    returns: pd.DataFrame,
    weights: dict[str, float],
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> float:
    """Weighted avg asset vol / portfolio vol, using shrunk annualized covariance.

    Delegates to `portfolio.services.diversification`, which owns this formula.
    This module used to carry a second, independently written copy of it -- two
    definitions of one published number is a disagreement waiting to surface.
    """
    cov = annualized_cov(returns, weights, periods_per_year)
    if cov is None:
        return 1.0
    return _finite(diversification.diversification_ratio(weights, cov), 1.0)


# Calendar-day pad before the first requested day. Only needs to reach the
# previous trading day so the first in-window return is defined; the loader
# also extends to the last row before the pad, so a long closure cannot NaN it.
_INDEX_WINDOW_PAD_DAYS = 45
_INDEX_CLOSES_CACHE_TTL = 3600


def _index_daily_closes(since_jalali: str | None, as_of_jalali: str | None) -> pd.Series | None:
    """Last TEDPIX value of each day in [since, as_of], Gregorian-indexed.

    Cached under the table's newest id. Both writers (`ingest_market_index`,
    `ingest_tedpix_history`) only insert -- conflicts are skipped, never
    updated -- so a new max id is exactly "the table changed" and the cache
    can never serve a stale series. Called once per metrics series (portfolio,
    every holding, every class), it used to read and convert the whole table
    each time: ~25 full loads per Risk request, hundreds per MyOptimal.
    """
    from django.core.cache import cache
    from django.db.models import Max
    from marketdata.models import MarketIndexData
    import jdatetime

    version = MarketIndexData.objects.aggregate(v=Max("id"))["v"]
    if version is None:
        return None
    key = f"diag:index-closes:v1:{since_jalali or '-'}:{as_of_jalali or '-'}:{version}"
    cached = cache.get(key)
    if cached is not None:
        return cached if len(cached) else None

    qs = MarketIndexData.objects.all()
    if as_of_jalali is not None:
        qs = qs.filter(date__lte=as_of_jalali)
    if since_jalali is not None:
        previous = (
            qs.filter(date__lt=since_jalali).order_by("-date").values_list("date", flat=True).first()
        )
        qs = qs.filter(date__gte=previous or since_jalali)

    # Ordered by (date, time): the last row of a day overwrites the earlier
    # ones, so each date maps to its final value -- as the full load did.
    last_by_date: dict[str, float] = {}
    for date, value in qs.order_by("date", "time").values_list("date", "index_overall"):
        last_by_date[date] = value

    records: dict[dt.date, float] = {}
    for date, value in last_by_date.items():
        try:
            parts = [int(p) for p in date.split("-")]
            records[jdatetime.date(parts[0], parts[1], parts[2]).togregorian()] = float(value)
        except Exception:
            continue
    series = pd.Series(records, dtype=float).sort_index()
    cache.set(key, series, timeout=_INDEX_CLOSES_CACHE_TTL)
    return series if len(series) else None


def _load_index_returns(target_index: pd.Index, as_of: dt.datetime | None = None) -> pd.Series | None:
    """Load index return series from MarketIndexData, aligned with target_index."""
    from django.conf import settings
    if not getattr(settings, "HISTORICAL_BENCHMARK_ENABLED", False):
        return None
    from portfolio.services.returns import to_jalali_str

    as_of_jalali = to_jalali_str(as_of) if as_of is not None else None
    since_jalali = None
    if len(target_index):
        first = pd.Timestamp(target_index.min())
        if not pd.isna(first):
            since_jalali = to_jalali_str(first.date() - dt.timedelta(days=_INDEX_WINDOW_PAD_DAYS))

    s = _index_daily_closes(since_jalali, as_of_jalali)
    if s is None or len(s) < 2:
        return None

    s_returns = s.pct_change(fill_method=None)
    s_returns.index = pd.to_datetime(s_returns.index, utc=True).normalize()
    return s_returns.reindex(target_index)



def _aggregate_holdings(valuation: dict | None) -> list[dict]:
    """Merge valuation items across accounts, one row per asset key."""
    if not valuation:
        return []
    accounts = valuation.get("accounts") or [{"items": valuation.get("items", [])}]
    by_key: dict[str, dict] = {}
    for acct in accounts:
        for item in acct.get("items") or []:
            key = item["key"]
            raw_val = item.get("value")
            val = Decimal(str(raw_val)) if raw_val is not None else Decimal("0")
            if key in by_key:
                by_key[key]["value"] += val
            else:
                by_key[key] = {
                    "key": key,
                    "name": item.get("asset") or key,
                    "asset_class": item.get("class") or "Unknown",
                    "value": val,
                    "is_house": bool(item.get("is_house")),
                    "is_manual": bool(item.get("is_manual")),
                    "priced": raw_val is not None and val > 0,
                }
    return list(by_key.values())


def _exclusion_map(excluded: list[dict]) -> dict[str, str]:
    return {row["key"]: row.get("reason", "excluded") for row in excluded if row.get("key")}


def _warning_map(warnings: list[dict]) -> dict[str, list[dict]]:
    """Group `daily_returns_matrix` warnings by asset key.

    A warning means the column survived but carries a caveat (proxied series,
    failed integrity gate, short history, thin coverage). It annotates a `ready`
    row rather than removing it -- silence about an asset you own is worse than
    a number with a footnote.
    """
    grouped: dict[str, list[dict]] = {}
    for row in warnings or []:
        if row.get("key"):
            grouped.setdefault(row["key"], []).append(row)
    return grouped


def _benchmark_metrics_for_series(
    port_series: pd.Series,
    *,
    as_of_dt,
    risk_free_annual: float,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> dict:
    if port_series.empty:
        return {
            "benchmark_status": "unavailable",
            "benchmark_status_reason": "no benchmark index history for this window",
        }
    index_returns = _load_index_returns(port_series.index, as_of=as_of_dt)
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
    if index_returns is None:
        return {
            "benchmark_status": "unavailable",
            "benchmark_status_reason": "no benchmark index history for this window",
        }
    beta = 0.0
    cov_matrix = np.cov(port_for_benchmark, index_returns)
    benchmark_var = np.var(index_returns, ddof=1)
    if benchmark_var > 0:
        beta = cov_matrix[0][1] / benchmark_var
    ann_port_return = np.mean(port_for_benchmark) * periods_per_year
    ann_index_return = np.mean(index_returns) * periods_per_year
    alpha = ann_port_return - (
        risk_free_annual + beta * (ann_index_return - risk_free_annual)
    )
    active_returns = port_for_benchmark - index_returns
    tracking_error = np.std(active_returns, ddof=1) * np.sqrt(periods_per_year)
    info_ratio = 0.0
    if tracking_error > 0:
        info_ratio = (np.mean(active_returns) * periods_per_year) / tracking_error
    return {
        "beta": _finite(beta),
        "alpha": _finite(alpha),
        "tracking_error": _finite(tracking_error),
        "information_ratio": _finite(info_ratio),
    }


def _metrics_from_series(
    port_series: pd.Series,
    returns: pd.DataFrame,
    weights: dict[str, float],
    *,
    risk_free_annual: float,
    as_of_dt,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> dict | None:
    if port_series.empty or len(port_series.index) < 2:
        return None
    mdd, days_under = _max_drawdown(port_series)
    calmar, calmar_window_days = _calmar(port_series, periods_per_year)
    var95, cvar95 = _historical_var_cvar(port_series, alpha=0.95)
    benchmark = _benchmark_metrics_for_series(
        port_series,
        as_of_dt=as_of_dt,
        risk_free_annual=risk_free_annual,
        periods_per_year=periods_per_year,
    )
    return {
        "annualized_volatility": _finite(
            _annualized_volatility(port_series, periods_per_year)
        ),
        "sharpe": _finite(_sharpe(port_series, risk_free_annual, periods_per_year)),
        "sortino": _finite(_sortino(port_series, risk_free_annual, periods_per_year)),
        "max_drawdown": _finite(mdd),
        "days_under_water": int(days_under),
        "calmar": calmar,
        "calmar_window_days": calmar_window_days,
        "historical_var_95_daily": _finite(var95),
        "historical_cvar_95_daily": cvar95,
        "diversification_ratio": _finite(
            _diversification_ratio(returns, weights, periods_per_year)
            if not returns.empty else 1.0
        ),
        "current_drawdown": _finite(_current_drawdown(port_series)),
        "concentration_hhi": _finite(_concentration_hhi(weights)),
        "best_day": _finite(_best_worst_day(port_series)[0]),
        "worst_day": _finite(_best_worst_day(port_series)[1]),
        "observations": len(port_series.index),
        "periods_per_year": _finite(periods_per_year, float(TRADING_DAYS_PER_YEAR)),
        "mean_weight_covered": _finite(
            port_series.attrs.get("mean_weight_covered", 1.0), 1.0
        ),
        "period_returns": _period_returns(port_series),
        **benchmark,
    }


def _asset_status(
    holding: dict,
    exclusion_map: dict[str, str],
    returns: pd.DataFrame,
) -> tuple[str, str | None]:
    key = holding["key"]
    if holding.get("is_house"):
        return "not_applicable", "real_estate_valuation_only"
    if not holding.get("priced"):
        return "excluded", "missing_price"
    if key in exclusion_map:
        return "excluded", exclusion_map[key]
    if key not in returns.columns:
        return "excluded", "no_return_history"
    series = returns[key].dropna()
    if len(series.index) < 2:
        return "excluded", "insufficient_history"
    return "ready", None


def _build_by_asset(
    holdings: list[dict],
    returns: pd.DataFrame,
    excluded: list[dict],
    current_weights: dict[str, float],
    weights_used: dict[str, float],
    full_weights: dict[str, float],
    *,
    risk_free_annual: float,
    as_of_dt,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
    warnings: list[dict] | None = None,
) -> list[dict]:
    exclusion_map = _exclusion_map(excluded)
    warning_map = _warning_map(warnings or [])
    rows = []
    for holding in sorted(holdings, key=lambda h: (-float(h["value"]), h["key"])):
        key = holding["key"]
        status, reason = _asset_status(holding, exclusion_map, returns)
        metrics = None
        observations = 0
        if status == "ready":
            series = returns[key].dropna()
            observations = len(series.index)
            metrics = _metrics_from_series(
                series,
                returns,
                {key: 1.0},
                risk_free_annual=risk_free_annual,
                as_of_dt=as_of_dt,
                periods_per_year=periods_per_year,
            )
        key_warnings = warning_map.get(key, [])
        proxied_from = next(
            (w.get("detail") for w in key_warnings if w.get("reason") == "proxied"),
            None,
        )
        rows.append({
            "key": key,
            "name": holding["name"],
            "asset_class": holding["asset_class"],
            "weight_in_portfolio": _finite(full_weights.get(key, 0.0)),
            "weight_in_analyzable": _finite(weights_used.get(key, 0.0)),
            "value_tomans": str(holding["value"]),
            "status": status,
            "status_reason": reason,
            "observations": observations,
            "warnings": [w["reason"] for w in key_warnings],
            "proxied_from": proxied_from,
            "metrics": metrics,
        })
    return rows


def _build_by_asset_class(
    holdings: list[dict],
    by_asset: list[dict],
    returns: pd.DataFrame,
    full_weights: dict[str, float],
    *,
    risk_free_annual: float,
    as_of_dt,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> list[dict]:
    classes: dict[str, list[dict]] = {}
    for holding in holdings:
        classes.setdefault(holding["asset_class"], []).append(holding)

    rows = []
    for asset_class in sorted(classes.keys()):
        class_holdings = classes[asset_class]
        held_count = len(class_holdings)
        class_value = sum(h["value"] for h in class_holdings if h.get("priced"))
        asset_rows = [row for row in by_asset if row["asset_class"] == asset_class]
        ready_rows = [row for row in asset_rows if row["status"] == "ready"]
        analyzable_count = len(ready_rows)

        if asset_class == "Real Estate":
            status = "not_applicable"
            metrics = None
        else:
            class_weights = {
                row["key"]: row["weight_in_portfolio"]
                for row in ready_rows
                if row["weight_in_portfolio"] > 0
            }
            total_w = sum(class_weights.values())
            if total_w > 0:
                class_weights = {k: v / total_w for k, v in class_weights.items()}
            if not class_weights:
                status = "insufficient"
                metrics = None
            else:
                class_series = _portfolio_returns(returns, class_weights) if not returns.empty else pd.Series(dtype=float)
                metrics = _metrics_from_series(
                    class_series,
                    returns,
                    class_weights,
                    risk_free_annual=risk_free_annual,
                    as_of_dt=as_of_dt,
                    periods_per_year=periods_per_year,
                )
                excluded_in_class = len(asset_rows) - analyzable_count
                if excluded_in_class and analyzable_count:
                    status = "partial"
                elif analyzable_count:
                    status = "ready"
                else:
                    status = "insufficient"

        rows.append({
            "asset_class": asset_class,
            "weight_in_portfolio": _finite(sum(full_weights.get(h["key"], 0.0) for h in class_holdings)),
            "held_count": held_count,
            "analyzable_count": analyzable_count,
            "value_tomans": str(class_value),
            "status": status,
            "metrics": metrics,
        })
    return rows


def _build_coverage(by_asset: list[dict], holdings: list[dict], full_total: Decimal) -> dict:
    total_holdings = len(holdings)
    ready = [row for row in by_asset if row["status"] == "ready"]
    excluded = [row for row in by_asset if row["status"] == "excluded"]
    not_applicable = [row for row in by_asset if row["status"] == "not_applicable"]
    analyzable_value = sum(Decimal(row["value_tomans"]) for row in ready)
    excluded_by_reason: dict[str, int] = {}
    for row in excluded:
        reason = row.get("status_reason") or "excluded"
        excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1
    warned_by_reason: dict[str, int] = {}
    for row in by_asset:
        for reason in row.get("warnings") or []:
            warned_by_reason[reason] = warned_by_reason.get(reason, 0) + 1

    value_analyzable_pct = 0.0
    if full_total > 0:
        value_analyzable_pct = _finite(float(analyzable_value / full_total))

    # `proxied` is a modeling choice the user opted into, not a defect; every
    # other warning says the underlying data is thinner than it looks, and a 37%
    # position whose integrity gate failed must not read "Healthy".
    quality_warnings = sum(
        count for reason, count in warned_by_reason.items() if reason != "proxied"
    )
    failed = len(excluded)
    if failed and not ready:
        health = "unhealthy"
    elif failed or quality_warnings:
        health = "degraded"
    else:
        health = "healthy"

    return {
        "health": health,
        "total_holdings": total_holdings,
        "analyzable_holdings": len(ready),
        "excluded_holdings": len(excluded),
        "not_applicable_holdings": len(not_applicable),
        "value_analyzable_pct": value_analyzable_pct,
        # Share of the whole book the portfolio metrics actually describe. The
        # metrics renormalize over analyzable assets, so without this the reader
        # cannot tell a whole-portfolio number from a third of one.
        "analyzed_weight_pct": _finite(
            sum(row["weight_in_portfolio"] for row in ready)
        ),
        "excluded_by_reason": excluded_by_reason,
        "warned_by_reason": warned_by_reason,
    }

def portfolio_diagnostics(
    current_weights: dict[str, float],
    total_value_tomans: Decimal,
    *,
    user=None,
    history_days: int = 180,
    as_of=None,
    universe: list[str] | None = None,
    basis: str = "nominal_toman",
    valuation: dict | None = None,
) -> dict:
    """Compute portfolio, per-asset, and per-class diagnostics.

    `current_weights` is liquid weights only (real estate excluded upstream).
    `valuation` (optional) supplies all held assets for breakdown rows.
    """
    from portfolio.services.returns import normalize_as_of
    as_of_dt = normalize_as_of(as_of)
    basis = normalize_basis(basis)
    import jdatetime
    from django.utils import timezone

    rate_date = as_of_dt or timezone.now()
    rate_year = jdatetime.date.fromgregorian(date=rate_date.date()).year
    risk_free_annual = settings.RATE_FOR(rate_year)

    # Held assets drive the query, not the whole catalog: they set the universe
    # (so the panel is the user's book) and `held_keys` (so the market-universe
    # screening gates annotate their columns instead of deleting them).
    holdings = _aggregate_holdings(valuation)
    held_keys = frozenset(
        h["key"] for h in holdings if not h.get("is_house")
    )

    returns, excluded = daily_returns_matrix(
        history_days=history_days,
        as_of=as_of_dt,
        universe=universe if universe is not None else (sorted(held_keys) or None),
        basis=basis,
        held_keys=held_keys,
    )
    frequency = float(returns.attrs.get("periods_per_year", TRADING_DAYS_PER_YEAR))
    warnings = returns.attrs.get("warnings", [])
    port_series = _portfolio_returns(returns, current_weights) if not returns.empty else pd.Series(dtype=float)
    weights_used = port_series.attrs.get("weights_used", current_weights)

    metrics = _metrics_from_series(
        port_series,
        returns,
        weights_used,
        risk_free_annual=risk_free_annual,
        as_of_dt=as_of_dt,
        periods_per_year=frequency,
    ) or {
        "annualized_volatility": 0.0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "max_drawdown": 0.0,
        "days_under_water": 0,
        "calmar": None,
        "calmar_window_days": 0,
        "historical_var_95_daily": 0.0,
        "historical_cvar_95_daily": None,
        "diversification_ratio": 1.0,
        "current_drawdown": 0.0,
        "concentration_hhi": _concentration_hhi(current_weights),
        "best_day": 0.0,
        "worst_day": 0.0,
        "observations": 0,
        "periods_per_year": frequency,
        "mean_weight_covered": 0.0,
        "period_returns": {},
        "benchmark_status": "unavailable",
        "benchmark_status_reason": "no benchmark index history for this window",
    }

    period_returns = metrics.pop("period_returns", {})
    hhi = metrics.pop("concentration_hhi", _concentration_hhi(current_weights))
    eligible_assets = list(returns.columns) if not returns.empty else []

    full_total = sum((h["value"] for h in holdings if h.get("priced")), Decimal("0"))
    if full_total <= 0:
        full_total = Decimal(str(total_value_tomans))
    full_weights = {
        h["key"]: float(h["value"] / full_total)
        for h in holdings
        if h.get("priced") and full_total > 0
    }

    by_asset = _build_by_asset(
        holdings,
        returns,
        excluded + port_series.attrs.get("dropped_assets", []),
        current_weights,
        weights_used,
        full_weights,
        risk_free_annual=risk_free_annual,
        as_of_dt=as_of_dt,
        periods_per_year=frequency,
        warnings=warnings,
    )
    by_asset_class = _build_by_asset_class(
        holdings,
        by_asset,
        returns,
        full_weights,
        risk_free_annual=risk_free_annual,
        as_of_dt=as_of_dt,
        periods_per_year=frequency,
    )
    coverage = _build_coverage(by_asset, holdings, full_total)

    # Where the risk actually comes from, for the CURRENT book. `optimize()`
    # reports the same block for its target, but that endpoint answers "what
    # should I hold?"; this one answers "what am I carrying right now?", which is
    # the question the household view asks and could not previously answer.
    #
    # `risk_contributions` is the payload's most actionable number and rarely
    # matches the weight split: a small, volatile, uncorrelated sleeve and a
    # large one that moves with everything else can carry identical risk, and
    # only this decomposition separates them. `mean_weight_covered` travels with
    # it deliberately -- a decomposition over 60% of the book must not be read as
    # if it covered all of it.
    cov_annual = annualized_cov(returns, weights_used, frequency)
    # How many days that covariance was actually estimated on. It is the panel
    # INTERSECTION (annualized_cov drops any day where a weighted asset is
    # missing), so it is smaller than the panel and smaller than the portfolio
    # series -- and it is the binding sample size for every number derived from
    # the matrix: the risk shares, the effective-bet count, the heatmap. A
    # correlation's standard error is ~1/sqrt(n) of THIS n, not of the window
    # the user asked for, and reporting the larger figure overstates how
    # separable the top of the ranking is.
    cov_observations = (
        int(len(returns[list(cov_annual.index)].dropna(how="any").index))
        if cov_annual is not None
        else 0
    )
    class_map = {h["key"]: h["asset_class"] for h in holdings}
    diversification_block = (
        diversification.diversification_report(weights_used, cov_annual, class_map)
        if cov_annual is not None
        else {
            "effective_bets": 0.0,
            "effective_holdings": _finite(
                diversification.effective_holdings(weights_used)
            ),
            "diversification_ratio": 1.0,
            "risk_contributions": {},
            "concentration_gap": [],
            "n_assets": len([v for v in weights_used.values() if float(v) > 0]),
            "unavailable_reason": "need 2+ assets with overlapping history",
        }
    )
    diversification_block["mean_weight_covered"] = metrics.get(
        "mean_weight_covered", 0.0
    )

    # The correlation structure behind the numbers above. Diversification is a
    # scalar summary of this matrix, so shipping both lets the UI show WHY the
    # effective-bet count is what it is -- a block of assets that all move
    # together reads instantly here and not at all from a single ratio.
    correlation = correlation_matrix(returns_df=returns)

    real_estate: dict = {"value_tomans": "0", "share_of_total": 0.0}
    if user is not None:
        re_value = _user_real_estate_value(user)
        total_decimal = Decimal(str(total_value_tomans)) + re_value
        share = float(re_value / total_decimal) if total_decimal > 0 else 0.0
        real_estate = {
            "value_tomans": str(re_value),
            "share_of_total": _finite(share),
        }

    portfolio_full = {
        "total_holdings": len(holdings),
        "total_value_tomans": str(full_total),
        "concentration_hhi": _finite(_concentration_hhi(full_weights)),
        "analyzable_value_pct": coverage["value_analyzable_pct"],
    }

    return {
        "basis": basis,
        "history_days": history_days,
        "risk_free_rate_annual": risk_free_annual,
        "risk_free_rate_jalali_year": rate_year,
        # settings.py describes this rate as "an ASSUMPTION, not a measured
        # yield" and says callers should surface that alongside any Sharpe. No
        # caller did, so a hand-maintained estimate was reading as a fact.
        "risk_free_rate_source": getattr(settings, "RISK_FREE_RATE_SOURCE", ""),
        "analysis_type": "hypothetical_fixed_weight_exposure",
        "current_weights": current_weights,
        "weights_used": weights_used,
        "weights_rescaled": port_series.attrs.get("weights_rescaled", False),
        "total_value_tomans": str(total_value_tomans),
        "eligible_assets": eligible_assets,
        "excluded_assets": excluded + port_series.attrs.get("dropped_assets", []),
        "data_window": {
            "start": returns.index.min().isoformat() if not returns.empty else None,
            "end": returns.index.max().isoformat() if not returns.empty else None,
            # Days the RISK numbers were estimated on, which is the intersection
            # the covariance saw -- not the panel's span and not the portfolio
            # series, both of which are longer. Falls back to the portfolio
            # series when there is no covariance to speak of (a single asset).
            "observations": cov_observations or len(port_series.index),
            "portfolio_observations": len(port_series.index),
            "panel_observations": int(len(returns.index)),
        },
        "asset_warnings": warnings,
        "periods_per_year": frequency,
        "metrics": metrics,
        "period_returns": period_returns,
        "rolling": _rolling_metrics(
            port_series, risk_free_annual, periods_per_year=frequency
        ),
        "real_estate": real_estate,
        "by_asset": by_asset,
        "by_asset_class": by_asset_class,
        "coverage": coverage,
        "portfolio_full": portfolio_full,
        "concentration_hhi": hhi,
        "diversification": diversification_block,
        "correlation": correlation,
    }

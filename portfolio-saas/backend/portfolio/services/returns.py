"""Daily-returns pipeline: warehouse history + Price rows -> daily return matrix.

This is the foundation for diagnostics and optimization. It is the only place
that touches pandas in the read path.

Source selection, per asset: if the marketdata warehouse has a real daily
series for it (>= MIN_DAILY_RETURNS rows in the window — DailyStockHistory via
`Asset.tse_symbol`, GoldCurrencyHistory via `Asset.brs_symbol`), use that;
otherwise fall back to the live Price table (2-min ticks resampled to daily).
One source per column — no splicing — which keeps the logic auditable. The
warehouse upgrade means optimization can see years of true closes instead of
however long the live feed has been running.

Returns are cached per version fingerprint (max ids of Price + both warehouse
tables), so both the 2-min fetch and the nightly sync auto-rotate the cache;
writers also call `invalidate_returns_cache` for belt-and-braces.

Two conventions matter here:
  * USD-quoted assets (`bitcoin_usd`, `gold_ounce_usd`) come through quoted in
    USD. Their Toman return is the USD return times the USD/Toman return, so we
    convert the *price* series by the daily-last `usd_cash` price BEFORE taking
    `pct_change()`. `usd_cash`, `usdt_irt` and `euro_cash` are already Tomans.
  * Real estate (`is_house=True`) is excluded — it has no daily price series.
"""
from __future__ import annotations

import datetime as dt

import jdatetime
import numpy as np
import pandas as pd
from django.core.cache import cache

from portfolio.models import Asset, Price

# How many days of price history to load by default (the buffer is so a one-day
# gap doesn't drop a return row off the front).
DEFAULT_HISTORY_DAYS = 180
# Drop an asset entirely if it has fewer non-NaN return rows than this.
MIN_DAILY_RETURNS = 30
# Cache key template — versioned by max(Price.id) so it auto-rotates on writes.
RETURNS_CACHE_KEY = "returns:daily:v{version}"
RETURNS_CACHE_TTL = 600
# Asset keys whose raw price is in USD; multiply through by usd_cash to Toman.
USD_QUOTED_KEYS = ("bitcoin_usd", "gold_ounce_usd")
# Extra days we fetch upstream of the window so resampling keeps the first row.
_HISTORY_BUFFER_DAYS = 7


def _price_version_fingerprint() -> str:
    """Monotonic fingerprint of every table feeding the panel.

    Combines max(id) of Price with max(id) of the two warehouse history tables,
    so both the 2-min live fetch and the nightly marketdata sync rotate the
    returns cache. Lazy import: portfolio -> marketdata is the allowed
    dependency direction (marketdata never imports portfolio's domain).
    """
    from marketdata.models import DailyStockHistory, GoldCurrencyHistory

    def _max_id(qs):
        return qs.order_by("-id").values_list("id", flat=True).first() or 0

    return "{}:{}:{}".format(
        hex(_max_id(Price.objects))[2:],
        hex(_max_id(DailyStockHistory.objects))[2:],
        hex(_max_id(GoldCurrencyHistory.objects))[2:],
    )


def _jalali_to_gregorian_index(dates: pd.Series) -> pd.DatetimeIndex:
    """Jalali "1403-10-19" strings -> tz-aware Gregorian DatetimeIndex.

    Warehouse rows store source-native Jalali dates; the returns matrix is
    indexed in Gregorian so it can align with the Price-table series.
    Unparseable dates become NaT (dropped by the caller).
    """
    def convert(value):
        try:
            y, m, d = (int(part) for part in str(value).split("-"))
            g = jdatetime.date(y, m, d).togregorian()
            return dt.datetime(g.year, g.month, g.day, tzinfo=dt.timezone.utc)
        except (ValueError, TypeError):
            return pd.NaT

    return pd.DatetimeIndex([convert(v) for v in dates])


def _warehouse_series(asset: Asset, cutoff: dt.datetime) -> pd.Series | None:
    """Daily close series for one asset from the warehouse, or None.

    DailyStockHistory (unadjusted `pl` close) for TSE assets, GoldCurrencyHistory
    for gold/currency/crypto. Returns None unless the series has at least
    MIN_DAILY_RETURNS rows inside the window — below that the sparse Price
    fallback is no worse, and one source per column keeps behavior predictable.
    """
    from marketdata.models import DailyStockHistory, GoldCurrencyHistory

    if asset.tse_symbol:
        rows = (
            DailyStockHistory.objects
            .filter(symbol=asset.tse_symbol, is_adjusted=False)
            .order_by("date")
            .values_list("date", "pl")
        )
    elif asset.brs_symbol:
        rows = (
            GoldCurrencyHistory.objects
            .filter(symbol=asset.brs_symbol)
            .order_by("date")
            .values_list("date", "close_price")
        )
    else:
        return None
    if not rows:
        return None

    dates, closes = zip(*rows)
    series = pd.Series(
        pd.to_numeric(pd.Series(closes), errors="coerce").values,
        index=_jalali_to_gregorian_index(pd.Series(dates)),
    )
    series = series[series.index.notna()]
    series = series[series > 0]
    series = series[series.index >= cutoff]
    if len(series) < MIN_DAILY_RETURNS:
        return None
    # Collapse duplicate days (adjusted/unadjusted overlap edge cases): keep last.
    return series.groupby(series.index).last()


def _load_price_panel(history_days: int) -> pd.DataFrame:
    """Per-asset daily close panel: warehouse series preferred, Price fallback.

    Columns are asset keys, indexed by (Gregorian) date. NaN where an asset had
    no row that day.
    """
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(
        days=history_days + _HISTORY_BUFFER_DAYS
    )

    assets = list(Asset.objects.filter(is_active=True).exclude(is_house=True))
    warehouse_cols: dict[str, pd.Series] = {}
    fallback_keys: list[str] = []
    for asset in assets:
        series = _warehouse_series(asset, cutoff)
        if series is not None:
            warehouse_cols[asset.key] = series
        else:
            fallback_keys.append(asset.key)

    fallback_panel = _load_live_price_panel(cutoff, fallback_keys)

    if not warehouse_cols:
        return fallback_panel
    panel = pd.DataFrame(warehouse_cols)
    panel.index = panel.index.normalize()
    if not fallback_panel.empty:
        panel = panel.join(fallback_panel, how="outer")
    return panel.sort_index()


def _load_live_price_panel(cutoff: dt.datetime, keys: list[str]) -> pd.DataFrame:
    """The original Price-table loader, restricted to the given asset keys."""
    if not keys:
        return pd.DataFrame()
    rows = (
        Price.objects.filter(
            asset__is_active=True, asset__key__in=keys, fetched_at__gte=cutoff
        )
        .exclude(asset__is_house=True)
        .select_related("asset")
        .order_by("asset__key", "fetched_at")
        .values("asset__key", "fetched_at", "price")
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame.from_records(rows)
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["price"])
    df = df[df["price"] > 0]

    panel = (
        df.pivot_table(
            index="fetched_at",
            columns="asset__key",
            values="price",
            aggfunc="last",
        )
        .resample("1D")
        .last()
    )
    panel.index = panel.index.normalize()
    return panel


def _convert_usd_to_toman(panel: pd.DataFrame) -> pd.DataFrame:
    """Multiply USD-quoted columns by the daily-last usd_cash price (in place).

    Operates on the price panel BEFORE returns are taken, so the resulting
    daily return correctly reflects both the USD move and the FX move. Forward-
    fills usd_cash so a missing day still uses the most recent rate.
    """
    if "usd_cash" not in panel.columns:
        return panel
    fx = panel["usd_cash"].ffill()
    for key in USD_QUOTED_KEYS:
        if key in panel.columns:
            panel[key] = panel[key] * fx
    return panel


def _build_returns_matrix(panel: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Price panel -> (daily simple returns, excluded list).

    Excludes any asset with fewer than MIN_DAILY_RETURNS non-NaN return rows.
    """
    if panel.empty:
        return pd.DataFrame(), []

    returns = panel.pct_change()
    excluded: list[dict] = []
    keep: list[str] = []
    for key in returns.columns:
        non_nan = int(returns[key].notna().sum())
        if non_nan < MIN_DAILY_RETURNS:
            excluded.append(
                {"key": key, "reason": "insufficient_history", "days": non_nan}
            )
        else:
            keep.append(key)
    returns = returns[keep] if keep else pd.DataFrame(index=returns.index)
    return returns, excluded


def daily_returns_matrix(
    *, history_days: int = DEFAULT_HISTORY_DAYS
) -> tuple[pd.DataFrame, list[dict]]:
    """Return `(daily_returns_df, excluded)` for the eligible universe.

    Cached per price-version fingerprint. On a cache miss, builds the matrix
    from one bounded Price query and stores it serialized under the versioned
    key (TTL 600s). The df is indexed by date, columns are asset keys, values
    are daily simple returns (float).
    """
    version = _price_version_fingerprint()
    key = RETURNS_CACHE_KEY.format(version=version)
    cached = cache.get(key)
    if cached is not None:
        # Stored as {"columns": [...], "index": [iso...], "data": [[col0,col1,...], ...]}.
        df = pd.DataFrame(
            data=cached["data"],
            index=pd.to_datetime(cached["index"], utc=True),
            columns=cached["columns"],
        )
        return df, cached["excluded"]

    panel = _load_price_panel(history_days)
    panel = _convert_usd_to_toman(panel)
    returns, excluded = _build_returns_matrix(panel)

    if returns.empty:
        payload = {"columns": [], "index": [], "data": []}
    else:
        # Replace NaN with None so locmem (which uses pickle) round-trips fine
        # and a later JSON encoder can also handle the values directly.
        payload = {
            "columns": list(returns.columns),
            "index": [d.isoformat() for d in returns.index],
            "data": [
                [None if (isinstance(v, float) and not np.isfinite(v)) else float(v)
                 for v in row]
                for row in returns.to_numpy()
            ],
        }
    cache.set(key, {**payload, "excluded": excluded}, timeout=RETURNS_CACHE_TTL)
    return returns, excluded


def correlation_matrix() -> dict:
    """Correlation payload for the eligible universe from the returns df.

    NaN correlations (assets with no overlap) become 0 so the matrix is dense
    and JSON-serializable.
    """
    df, _ = daily_returns_matrix()
    if df.empty:
        return {"assets": [], "matrix": []}
    corr = df.corr().fillna(0.0)
    corr = np.nan_to_num(corr.to_numpy(), nan=0.0)
    return {"assets": list(df.columns), "matrix": corr.tolist()}


def invalidate_returns_cache() -> None:
    """Best-effort delete of the cached returns matrix for the current version.

    Called from the fetch task after each write. The next reader recomputes.
    """
    try:
        cache.delete(RETURNS_CACHE_KEY.format(version=_price_version_fingerprint()))
    except Exception:  # cache is best-effort; never crash a fetch on it
        pass


def eligible_universe_keys() -> list[str]:
    """Convenience: active, non-house, non-manual-pending asset keys.

    Mirrors the filter used by the price panel so optimization diagnostics line
    up with what the returns matrix actually contains.
    """
    return list(
        Asset.objects.filter(is_active=True)
        .exclude(is_house=True)
        .order_by("key")
        .values_list("key", flat=True)
    )

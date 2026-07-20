"""Daily-returns pipeline: Price rows -> daily simple-return matrix.

This is the foundation for diagnostics and optimization. It is the only place
that touches pandas in the read path. Returns are cached per price-version
fingerprint, so a new fetch (which bulk-creates Price rows, raising
`max(Price.id)`) auto-rotates the cache; the fetch task also calls
`invalidate_returns_cache` for belt-and-braces.

Two conventions matter here:
  * USD-quoted assets (`bitcoin_usd`, `gold_ounce_usd`) come through quoted in
    USD. Their Toman return is the USD return times the USD/Toman return, so we
    convert the *price* series by the daily-last `usd_cash` price BEFORE taking
    `pct_change()`. `usd_cash`, `usdt_irt` and `euro_cash` are already Tomans.
  * Real estate (`is_house=True`) is excluded — it has no daily price series.
"""
from __future__ import annotations

import datetime as dt

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
    """Monotonic fingerprint of the price table: hex of the max id (0 -> '0').

    Bumps on every `Price.objects.bulk_create` in the fetch task, so it tracks
    exactly the writes that change what the returns matrix would contain.
    """
    max_id = Price.objects.order_by("-id").values_list("id", flat=True).first()
    return hex(max_id or 0)[2:] or "0"


def _load_price_panel(history_days: int) -> pd.DataFrame:
    """One bounded query -> DataFrame of daily-LAST price per active non-house asset.

    Columns are asset keys, indexed by date. NaN where an asset had no row that
    day (the typical case — assets are not all fetched at the same cadence).
    """
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(
        days=history_days + _HISTORY_BUFFER_DAYS
    )
    rows = (
        Price.objects.filter(asset__is_active=True, fetched_at__gte=cutoff)
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

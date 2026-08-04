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
from .deflator import normalize_basis, to_basis

# How many days of price history to load by default (the buffer is so a one-day
# gap doesn't drop a return row off the front).
DEFAULT_HISTORY_DAYS = 180
# Drop an asset entirely if it has fewer non-NaN return rows than this.
MIN_DAILY_RETURNS = 30
# Cache key template — versioned by max(Price.id) so it auto-rotates on writes.
RETURNS_CACHE_KEY = "returns:daily:{history_days}d:v{version}"
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
    from marketdata.models import DailyStockHistory, GoldCurrencyHistory, MarketCandle

    def _max_id(qs):
        return qs.order_by("-id").values_list("id", flat=True).first() or 0

    return "{}:{}:{}:{}".format(
        hex(_max_id(Price.objects))[2:],
        hex(_max_id(DailyStockHistory.objects))[2:],
        hex(_max_id(GoldCurrencyHistory.objects))[2:],
        hex(_max_id(MarketCandle.objects))[2:],
    )


def _jalali_to_gregorian_index(dates: pd.Series) -> pd.DatetimeIndex:
    """Jalali "1403-10-19" strings -> tz-aware Gregorian DatetimeIndex.

    Warehouse rows store source-native Jalali dates; the returns matrix is
    indexed in Gregorian so it can align with the Price-table series.
    Unparseable dates become NaT (dropped by the caller).
    """
    def convert(value):
        try:
            y, m, d = (int(part) for part in str(value).split(" ")[0].split("-"))
            g = jdatetime.date(y, m, d).togregorian()
            return dt.datetime(g.year, g.month, g.day, tzinfo=dt.timezone.utc)
        except (ValueError, TypeError):
            return pd.NaT

    return pd.DatetimeIndex([convert(v) for v in dates])


def normalize_as_of(as_of) -> dt.datetime | None:
    """Normalize as_of to a timezone-aware datetime."""
    if as_of is None:
        return None
    if isinstance(as_of, str):
        try:
            if "T" in as_of:
                return dt.datetime.fromisoformat(as_of)
            else:
                y, m, d = (int(part) for part in as_of.split("-"))
                return dt.datetime(y, m, d, 23, 59, 59, tzinfo=dt.timezone.utc)
        except Exception:
            return None
    if isinstance(as_of, dt.date) and not isinstance(as_of, dt.datetime):
        return dt.datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=dt.timezone.utc)
    if isinstance(as_of, dt.datetime):
        if as_of.tzinfo is None:
            return as_of.replace(tzinfo=dt.timezone.utc)
        return as_of
    return None


def to_jalali_str(greg_date: dt.date | dt.datetime) -> str:
    """Convert a Gregorian date/datetime to a Jalali YYYY-MM-DD string."""
    if isinstance(greg_date, dt.datetime):
        greg_date = greg_date.date()
    jday = jdatetime.date.fromgregorian(date=greg_date)
    return f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}"


def resolve_universe(
    universe: list[str] | None = None,
    *,
    as_of=None,
) -> list[dict]:
    """Resolve universe items to dicts with key, symbol, and source.

    Each item in resolved list has:
      * 'key': column key to use in the DataFrame (e.g. 'kama_stock' or symbol)
      * 'symbol': the warehouse symbol ('کاما', 'USD', etc.)
      * 'source': 'tse' or 'brs'
      * 'asset': Asset object if exists
    """
    from marketdata.models import InstrumentListingHistory, MarketInstrument

    resolved = []
    assets = {a.key: a for a in Asset.objects.filter(is_active=True).exclude(is_house=True)}
    asset_by_symbol = {}
    for a in assets.values():
        sym = a.tse_symbol or a.brs_symbol
        if sym:
            asset_by_symbol[sym] = a

    if universe is None:
        for a in assets.values():
            resolved.append({
                "key": a.key,
                "symbol": a.tse_symbol or a.brs_symbol or "",
                "source": "tse" if a.tse_symbol else "brs",
                "asset": a
            })
    else:
        for item in universe:
            if item in assets:
                a = assets[item]
                resolved.append({
                    "key": a.key,
                    "symbol": a.tse_symbol or a.brs_symbol or "",
                    "source": "tse" if a.tse_symbol else "brs",
                    "asset": a
                })
            elif item in asset_by_symbol:
                a = asset_by_symbol[item]
                resolved.append({
                    "key": a.key,
                    "symbol": item,
                    "source": "tse" if a.tse_symbol else "brs",
                    "asset": a
                })
            else:
                mi = MarketInstrument.objects.filter(symbol=item).first()
                if mi:
                    resolved.append({
                        "key": item,
                        "symbol": item,
                        "source": "tse" if mi.source == MarketInstrument.Source.TSETMC else "brs",
                        "asset": None
                    })
                else:
                    mi = MarketInstrument.objects.filter(symbol__iexact=item).first()
                    if mi:
                        resolved.append({
                            "key": item,
                            "symbol": mi.symbol,
                            "source": "tse" if mi.source == MarketInstrument.Source.TSETMC else "brs",
                            "asset": None
                        })
                    else:
                        resolved.append({
                            "key": item,
                            "symbol": item,
                            "source": "brs",
                            "asset": None
                        })
    resolved = list({item["key"]: item for item in reversed(resolved)}.values())[::-1]
    if as_of is None:
        return resolved
    cutoff = to_jalali_str(normalize_as_of(as_of))
    listing = {
        row.symbol: row
        for row in InstrumentListingHistory.objects.filter(
            symbol__in=[item["symbol"] for item in resolved]
        )
    }
    return [
        item
        for item in resolved
        if (
            item["symbol"] not in listing
            or (
                listing[item["symbol"]].eligible_from
                and listing[item["symbol"]].eligible_from <= cutoff
                and (
                    not listing[item["symbol"]].eligible_to
                    or listing[item["symbol"]].eligible_to > cutoff
                )
            )
        )
    ]


def get_universe_by_mode(mode: str, user=None, account=None) -> list[str] | None:
    """Resolve the list of symbol keys based on the universe mode."""
    from portfolio.models import Holding, WatchlistItem, Asset
    
    active_assets = list(Asset.objects.filter(is_active=True).exclude(is_house=True).values_list("key", flat=True))

    if mode == "held":
        res = []
        if account is not None:
            res = list(account.holdings.values_list("asset__key", flat=True))
        elif user is not None:
            res = list(Holding.objects.filter(account__user=user).values_list("asset__key", flat=True))
        
        if not res:
            return active_assets
        return res

    elif mode == "watchlist":
        held_keys = []
        if account is not None:
            held_keys = list(account.holdings.values_list("asset__key", flat=True))
            items = WatchlistItem.objects.filter(watchlist__account=account)
            forced_in = set(items.filter(force_include=True).values_list("symbol", flat=True))
            forced_ex = set(items.filter(force_exclude=True).values_list("symbol", flat=True))
            watchlist_keys = set(items.values_list("symbol", flat=True))
            
            res = (set(held_keys) | watchlist_keys | forced_in) - forced_ex
            res_list = list(res)
            return res_list if res_list else active_assets
        elif user is not None:
            account = user.accounts.first()
            if account:
                held_keys = list(account.holdings.values_list("asset__key", flat=True))
                items = WatchlistItem.objects.filter(watchlist__account=account)
                forced_in = set(items.filter(force_include=True).values_list("symbol", flat=True))
                forced_ex = set(items.filter(force_exclude=True).values_list("symbol", flat=True))
                watchlist_keys = set(items.values_list("symbol", flat=True))
                
                res = (set(held_keys) | watchlist_keys | forced_in) - forced_ex
                res_list = list(res)
                return res_list if res_list else active_assets
        return active_assets

    elif mode == "market":
        from marketdata.models import MarketInstrument
        if not MarketInstrument.objects.filter(eligible=True).exists():
            return active_assets

        from marketdata.universe import get_candidate_universe
        candidates, _ = get_candidate_universe()
        if account is not None:
            items = WatchlistItem.objects.filter(watchlist__account=account)
            forced_in = set(items.filter(force_include=True).values_list("symbol", flat=True))
            forced_ex = set(items.filter(force_exclude=True).values_list("symbol", flat=True))
            res = (set(candidates) | forced_in) - forced_ex
            return list(res)
        elif user is not None:
            account = user.accounts.first()
            if account:
                items = WatchlistItem.objects.filter(watchlist__account=account)
                forced_in = set(items.filter(force_include=True).values_list("symbol", flat=True))
                forced_ex = set(items.filter(force_exclude=True).values_list("symbol", flat=True))
                res = (set(candidates) | forced_in) - forced_ex
                return list(res)
        return candidates

    return None


def _returns_cache_key(history_days: int, as_of: dt.datetime | None, universe: list[str] | None, basis: str, version: str) -> str:
    import hashlib
    basis = normalize_basis(basis)
    if universe is None:
        univ_str = "default"
    else:
        sorted_univ = sorted(universe)
        univ_str = hashlib.md5(",".join(sorted_univ).encode("utf-8")).hexdigest()[:16]

    as_of_str = "latest" if as_of is None else as_of.date().isoformat()
    return f"returns:daily:{history_days}d:as_of:{as_of_str}:univ:{univ_str}:basis:{basis}:v{version}"



def _warehouse_series(
    symbol: str,
    source: str,
    cutoff: dt.datetime,
    as_of: dt.datetime | None = None,
) -> pd.Series | None:
    """Daily close series for one asset from the warehouse, or None.

    DailyStockHistory (unadjusted `pl` close) for TSE assets, GoldCurrencyHistory
    for gold/currency/crypto. Returns None unless the series has at least
    MIN_DAILY_RETURNS rows inside the window.
    """
    from marketdata.candles import candle_close_qs
    from marketdata.models import GoldCurrencyHistory

    as_of_jalali = None
    if as_of is not None:
        as_of_jalali = to_jalali_str(as_of)

    from marketdata.models import RejectedRecord
    if source == "tse":
        rejections = set(
            RejectedRecord.objects.filter(
                symbol=symbol,
                endpoint__in=[
                    "stock_candle_adjusted", "stock_candle_unadjusted",
                    "stock_history_adjusted", "stock_history_unadjusted",
                    "series:1d_adj", "series:1d_unadj"
                ]
            ).values_list("date", flat=True)
        )
    elif source == "brs":
        rejections = set(
            RejectedRecord.objects.filter(
                symbol=symbol,
                endpoint__in=[
                    "gold_daily", "crypto_daily", "commodity_daily",
                    "market_index_daily", "etf_nav_daily", "option_contract_daily"
                ]
            ).values_list("date", flat=True)
        )
    else:
        rejections = set()

    if source == "tse":
        qs = candle_close_qs(symbol, as_of=as_of_jalali)
        rows = qs.order_by("date_time").values_list("date_time", "close_price")
        rows = [r for r in rows if r[0].split()[0] not in rejections]
    elif source == "brs":
        qs = GoldCurrencyHistory.objects.filter(symbol=symbol, close_price__gt=0)
        if as_of_jalali is not None:
            qs = qs.filter(date__lte=as_of_jalali)
        rows = qs.order_by("date").values_list("date", "close_price")
        rows = [r for r in rows if r[0] not in rejections]
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
    # Collapse duplicate days: keep last.
    return series.groupby(series.index).last()


def _load_price_panel(
    history_days: int,
    as_of: dt.datetime | None = None,
    universe: list[str] | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """Per-asset daily close panel: warehouse series preferred, Price fallback.

    Columns are asset keys, indexed by (Gregorian) date. NaN where an asset had
    no row that day.
    """
    as_of_dt = normalize_as_of(as_of)
    if as_of_dt is not None:
        cutoff = as_of_dt - dt.timedelta(days=history_days + _HISTORY_BUFFER_DAYS)
    else:
        cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(
            days=history_days + _HISTORY_BUFFER_DAYS
        )

    from marketdata.candles import candle_close_qs
    from marketdata.models import SymbolIntegrity, GoldCurrencyHistory
    # A current nightly assessment must not leak into a historical cutoff. Its
    # window may contain observations that did not exist at that cutoff; the
    # bounded panel checks below are the point-in-time integrity gate instead.
    failed_symbols = {} if as_of_dt is not None else {
        si.symbol: si.reason for si in SymbolIntegrity.objects.filter(passes_gate=False)
    }

    # Resolve universe items
    resolved_univ = resolve_universe(universe, as_of=as_of_dt)

    # We must ensure usd_cash is loaded in the panel for currency conversion and basis conversion
    usd_cash_in_univ = any(x["key"] == "usd_cash" for x in resolved_univ)
    if not usd_cash_in_univ:
        usd_cash_resolved = resolve_universe(["usd_cash"], as_of=as_of_dt)[0]
        resolved_univ.append(usd_cash_resolved)

    # Separate by source for bulk querying
    tse_symbols = []
    brs_symbols = []
    for item in resolved_univ:
        if item["source"] == "tse":
            tse_symbols.append(item["symbol"])
        elif item["source"] == "brs":
            brs_symbols.append(item["symbol"])

    as_of_jalali = None
    if as_of_dt is not None:
        as_of_jalali = to_jalali_str(as_of_dt)

    from marketdata.models import RejectedRecord

    # Bulk query MarketCandle (TSE)
    tse_rows = []
    if tse_symbols:
        qs_tse = candle_close_qs(tse_symbols, as_of=as_of_jalali)
        tse_rows = list(qs_tse.order_by("symbol", "date_time").values_list("symbol", "date_time", "close_price"))

    # Bulk query GoldCurrencyHistory (BRS)
    brs_rows = []
    if brs_symbols:
        qs_brs = GoldCurrencyHistory.objects.filter(
            symbol__in=brs_symbols,
            close_price__gt=0,
        )
        if as_of_jalali is not None:
            qs_brs = qs_brs.filter(date__lte=as_of_jalali)
        brs_rows = list(qs_brs.order_by("symbol", "date").values_list("symbol", "date", "close_price"))

    cutoff_jalali = to_jalali_str(cutoff)
    rejections = set(
        RejectedRecord.objects.filter(
            symbol__in=tse_symbols + brs_symbols,
            date__gte=cutoff_jalali,
            endpoint__in=[
                "stock_candle_adjusted", "stock_candle_unadjusted",
                "stock_history_adjusted", "stock_history_unadjusted",
                "series:1d_adj", "series:1d_unadj",
                "gold_daily", "crypto_daily", "commodity_daily",
                "market_index_daily", "etf_nav_daily", "option_contract_daily"
            ]
        ).values_list("symbol", "date")
    )
    if rejections:
        tse_rows = [r for r in tse_rows if (r[0], r[1].split()[0]) not in rejections]
        brs_rows = [r for r in brs_rows if (r[0], r[1]) not in rejections]

    # Group data by symbol
    tse_data = {}
    for sym, dt_str, close in tse_rows:
        tse_data.setdefault(sym, []).append((dt_str, close))

    brs_data = {}
    for sym, d_str, close in brs_rows:
        brs_data.setdefault(sym, []).append((d_str, close))

    warehouse_cols: dict[str, pd.Series] = {}
    fallback_keys: list[str] = []
    gate_excluded = []

    for item in resolved_univ:
        key = item["key"]
        symbol = item["symbol"]
        source = item["source"]

        # Check data integrity gate
        if symbol in failed_symbols:
            gate_excluded.append({
                "key": key,
                "reason": "integrity_gate_failed",
                "detail": failed_symbols[symbol]
            })
            continue

        # Extract rows from bulk data
        rows_data = tse_data.get(symbol, []) if source == "tse" else brs_data.get(symbol, [])
        if not rows_data:
            fallback_keys.append(key)
            continue

        dates, closes = zip(*rows_data)
        series = pd.Series(
            pd.to_numeric(pd.Series(closes), errors="coerce").values,
            index=_jalali_to_gregorian_index(pd.Series(dates)),
        )
        series = series[series.index.notna()]
        series = series[series > 0]
        series = series[series.index >= cutoff]

        # Survivorship guard: check if asset was trading at as_of
        if as_of_dt is not None and not series.index.empty:
            max_date = series.index.max()
            if (as_of_dt - max_date).days > 30:
                gate_excluded.append({
                    "key": key,
                    "reason": "survivorship_guard_failed",
                    "detail": f"No price updates near as_of {as_of_dt.date()}"
                })
                continue

        if len(series) < MIN_DAILY_RETURNS:
            fallback_keys.append(key)
            continue

        series = series.groupby(series.index).last()
        warehouse_cols[key] = series

    fallback_panel = _load_live_price_panel(cutoff, as_of_dt, fallback_keys)
    for key in fallback_keys:
        if key not in fallback_panel.columns:
            gate_excluded.append({
                "key": key,
                "reason": "no_price_history",
                "detail": "No trustworthy price observations in the requested window",
            })

    if not warehouse_cols:
        return fallback_panel, gate_excluded
    panel = pd.DataFrame(warehouse_cols)
    panel.index = panel.index.normalize()
    if not fallback_panel.empty:
        panel = panel.join(fallback_panel, how="outer")
    return panel.sort_index(), gate_excluded


def _load_live_price_panel(cutoff: dt.datetime, as_of: dt.datetime | None, keys: list[str]) -> pd.DataFrame:
    """The original Price-table loader, restricted to the given asset keys and cutoff/as_of."""
    if not keys:
        return pd.DataFrame()
    
    qs = Price.objects.filter(
        asset__is_active=True, asset__key__in=keys, fetched_at__gte=cutoff, price__gt=0
    ).exclude(asset__is_house=True)

    if as_of is not None:
        qs = qs.filter(fetched_at__lte=as_of)

    rows = (
        qs.select_related("asset")
        .order_by("asset__key", "fetched_at")
        .values("asset__key", "fetched_at", "price")
    )
    if not rows:
        return pd.DataFrame()

    rows = list(rows)

    # Exclude RejectedRecord matches
    from django.utils import timezone
    from portfolio.models import Asset
    from marketdata.models import RejectedRecord
    from django.conf import settings
    from datetime import timedelta

    assets = {a.key: (a.tse_symbol or a.brs_symbol or "") for a in Asset.objects.filter(key__in=keys)}
    symbols = [s for s in assets.values() if s]

    rejections = set(
        RejectedRecord.objects.filter(
            symbol__in=symbols,
            date__gte=to_jalali_str(cutoff),
            endpoint__in=[
                "stock_candle_adjusted", "stock_candle_unadjusted",
                "stock_history_adjusted", "stock_history_unadjusted",
                "series:1d_adj", "series:1d_unadj",
                "gold_daily", "crypto_daily", "commodity_daily",
                "market_index_daily", "etf_nav_daily", "option_contract_daily"
            ]
        ).values_list("symbol", "date")
    )

    now_tz = timezone.now() if timezone.is_aware(timezone.now()) else timezone.now().replace(tzinfo=dt.timezone.utc)
    ref_time = as_of if as_of is not None else now_tz
    stale_limit = ref_time - timedelta(seconds=getattr(settings, "PRICE_STALE_THRESHOLD_SECONDS", 900))

    # Identify latest fetched_at per asset to check staleness
    latest_ticks = {}
    for r in rows:
        k = r["asset__key"]
        f = r["fetched_at"]
        if k not in latest_ticks or f > latest_ticks[k]:
            latest_ticks[k] = f

    is_live_query = (as_of is None) or ((now_tz - as_of).total_seconds() < 3600)

    valid_rows = []
    for r in rows:
        k = r["asset__key"]
        f = r["fetched_at"]
        sym = assets.get(k)
        jalali_date = to_jalali_str(f)

        # 1. Exclude stale live prices
        if is_live_query and latest_ticks[k] < stale_limit:
            continue

        # 2. Exclude RejectedRecord matches
        if (sym, jalali_date) in rejections:
            continue

        valid_rows.append(r)
    rows = valid_rows

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame.from_records(rows)
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["price"])

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
    fx = panel["usd_cash"].ffill(limit=5)
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

    filled = panel.ffill(limit=5)
    returns = filled.pct_change(fill_method=None)
    excluded: list[dict] = []
    keep: list[str] = []
    for key in returns.columns:
        missing = panel[key].isna().to_numpy()
        longest_gap = current_gap = 0
        for is_missing in missing:
            current_gap = current_gap + 1 if is_missing else 0
            longest_gap = max(longest_gap, current_gap)
        non_nan = int(returns[key].notna().sum())
        expected = max(len(returns.index) - 1, 0)
        coverage = non_nan / expected if expected else 0.0
        if longest_gap > 5:
            excluded.append({
                "key": key,
                "reason": "price_gap_exceeded",
                "max_gap_sessions": longest_gap,
            })
        elif non_nan < MIN_DAILY_RETURNS:
            excluded.append(
                {"key": key, "reason": "insufficient_history", "days": non_nan}
            )
        elif coverage < 0.90:
            excluded.append({
                "key": key,
                "reason": "insufficient_coverage",
                "observations": non_nan,
                "expected_sessions": expected,
                "coverage": coverage,
            })
        else:
            keep.append(key)
    returns = returns[keep] if keep else pd.DataFrame(index=returns.index)
    return returns, excluded


def daily_returns_matrix(
    *,
    history_days: int = DEFAULT_HISTORY_DAYS,
    as_of=None,
    universe: list[str] | None = None,
    basis: str = "nominal_toman"
) -> tuple[pd.DataFrame, list[dict]]:
    """Return `(daily_returns_df, excluded)` for the eligible universe.

    Cached per price-version fingerprint. On a cache miss, builds the matrix
    from one bounded Price query and stores it serialized under the versioned
    key (TTL 600s). The df is indexed by date, columns are asset keys, values
    are daily simple returns (float).
    """
    as_of_dt = normalize_as_of(as_of)
    basis = normalize_basis(basis)
    version = _price_version_fingerprint()
    key = _returns_cache_key(history_days, as_of_dt, universe, basis, version)
    cached = cache.get(key)
    if cached is not None:
        df = pd.DataFrame(
            data=cached["data"],
            index=pd.to_datetime(cached["index"], utc=True),
            columns=cached["columns"],
        )
        return df, cached["excluded"]

    panel, gate_excluded = _load_price_panel(history_days, as_of=as_of_dt, universe=universe)
    panel = _convert_usd_to_toman(panel)

    # Apply basis conversion
    if basis == "usd_denominated" and "usd_cash" in panel.columns:
        usd_series = panel["usd_cash"]
        for col in panel.columns:
            panel[col] = to_basis(panel[col], basis, usd_series=usd_series)
    elif basis == "usdt_denominated":
        usdt_series = panel.get("usdt_irt")
        usd_series = panel.get("usd_cash")
        series_to_use = usdt_series if (usdt_series is not None and not usdt_series.isna().all()) else usd_series
        if series_to_use is not None:
            for col in panel.columns:
                panel[col] = to_basis(panel[col], basis, usd_series=series_to_use)

    returns, excluded = _build_returns_matrix(panel)
    excluded.extend(gate_excluded)

    # Filter columns to only include the requested universe
    resolved_univ = resolve_universe(universe)
    requested_keys = [item["key"] for item in resolved_univ]
    keep = [k for k in requested_keys if k in returns.columns]
    returns = returns[keep] if keep else pd.DataFrame(index=returns.index)

    # Filter excluded list to only include requested universe keys
    requested_keys_set = set(requested_keys)
    excluded = [e for e in excluded if e.get("key") in requested_keys_set]

    if returns.empty:
        payload = {"columns": [], "index": [], "data": []}
    else:
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


def correlation_matrix(
    *,
    history_days: int = DEFAULT_HISTORY_DAYS,
    as_of=None,
    universe: list[str] | None = None,
    basis: str = "nominal_toman"
) -> dict:
    """Correlation payload for the eligible universe from the returns df.

    NaN correlations (assets with no overlap) become 0 so the matrix is dense
    and JSON-serializable.
    """
    df, _ = daily_returns_matrix(
        history_days=history_days,
        as_of=as_of,
        universe=universe,
        basis=basis
    )
    if df.empty:
        return {"assets": [], "matrix": []}
    eligible = [column for column in df if df[column].std(ddof=0) > 0]
    corr = df[eligible].corr(min_periods=MIN_DAILY_RETURNS)
    complete = [column for column in corr if corr.loc[column].notna().all()]
    corr = corr.loc[complete, complete]
    return {"assets": complete, "matrix": corr.to_numpy().tolist()}


def invalidate_returns_cache() -> None:
    """Best-effort delete of the cached returns matrix for the current version.

    Called from the fetch task after each write. The next reader recomputes.
    """
    try:
        version = _price_version_fingerprint()
        for history_days in (30, 90, DEFAULT_HISTORY_DAYS, 365):
            for basis in ("nominal_toman", "usd_denominated"):
                key = _returns_cache_key(history_days, None, None, basis, version)
                cache.delete(key)
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

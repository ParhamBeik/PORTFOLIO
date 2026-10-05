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
  * BRS history and verified foreign-seed Price rows enter in Toman after
    declared-unit conversion. TSE warehouse and verified live closes enter as
    Rial and are divided by `tse_close_to_toman()` on load. UNKNOWN-unit live
    foreign/TSE rows are excluded.
  * Real estate (`is_house=True`) is excluded — it has no daily price series.
"""
from __future__ import annotations

import datetime as dt
import hashlib

import jdatetime
import numpy as np
import pandas as pd
from django.core.cache import cache
from django.db.models import DecimalField, Max, Q
from django.db.models.functions import Coalesce

from marketdata.currency import tse_close_to_toman
from marketdata.integrity import (
    MAX_FORWARD_FILL_SESSIONS,
    MAX_OUTAGE_CALENDAR_DAYS,
    MIN_COVERAGE,
)
from marketdata.provenance import PRICE_SERIES_ENDPOINTS, rejected_pairs
from portfolio.models import Asset, Price, USD_QUOTED_KEYS, positive_price_q
from .deflator import normalize_basis, to_basis
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from zoneinfo import ZoneInfo
from marketdata.calendars import candle_close_qs, market_closure_days
from marketdata.models import (
    DailyStockHistory,
    GoldCurrencyHistory,
    InstrumentListingHistory,
    MarketCandle,
    MarketDailyBar,
    MarketInstrument,
    RejectedRecord,
    SymbolIntegrity,
)

# How many days of price history to load by default (the buffer is so a one-day
# gap doesn't drop a return row off the front).
DEFAULT_HISTORY_DAYS = 180
# Drop an asset entirely if it has fewer non-NaN return rows than this.
MIN_DAILY_RETURNS = 30
# Fallback annualization factor. Prefer `periods_per_year(index)`, which reads the
# panel's actual sampling frequency; this is only used when the index is too short
# to measure one.
TRADING_DAYS_PER_YEAR = 252
# Cache key template — versioned by max(Price.id) so it auto-rotates on writes.
RETURNS_CACHE_KEY = "returns:daily:{history_days}d:v{version}"
RETURNS_CACHE_TTL = 600
_TEHRAN = ZoneInfo("Asia/Tehran")
# Asset keys whose raw price is in USD; multiply through by usd_cash to Toman.
# Extra days we fetch upstream of the window so resampling keeps the first row.
_HISTORY_BUFFER_DAYS = 7
# MAX_OUTAGE_CALENDAR_DAYS lives in marketdata.integrity (imported above) --
# it used to be redefined here too, and the two copies could only drift out of
# sync by hand. See that module for why length alone cannot tell a closure
# from an ingest hole; `_closure_explained()` below asks the data instead.


def _returns_asset_keys(universe, held_keys=frozenset()):
    """The assets that can affect one returns panel, or all assets for None."""
    return set(universe) | set(held_keys) if universe is not None else None


def _price_version_fingerprint(asset_keys=None, *, include_price=True) -> str:
    """Monotonic fingerprint of the sources feeding one returns panel.

    Warehouse tables are symbol-keyed while the live fallback is asset-keyed.
    Passing `asset_keys` therefore narrows the question
    from "has anything anywhere been ingested" to "has anything this panel
    actually reads been ingested". That distinction is the whole point: the
    archive backfill writes `MarketCandle` continuously and will for another
    year, so a global fingerprint rotated faster than a request could be
    served. Every cache keyed on it -- the returns matrix, `optimize()`, and
    the whole MyOptimal response body -- therefore missed essentially always,
    and MyOptimal paid ~40 convex solves per request for a 300s cache that
    could never hold. `_returns_cache_key` already hashes the universe and
    `held_keys`, so the narrowing is consistent by construction.

    `asset_keys=None` keeps the old global behaviour, which is correct for the
    universe-wide callers (`invalidate_returns_cache`, warmup) where the panel
    genuinely is everything.

    Deliberately NOT cached, even though caching it would bound the cost of the
    scoped lookups. The fingerprint's whole job is to rotate the moment the
    data changes -- a previous fix specifically removed a 600s TTL here so a
    new spike rejection took effect immediately instead of waiting it out, and
    putting a TTL back would undo that under a different name. Scoping alone is
    what makes the caches downstream viable; the remaining per-call cost is
    eight scoped lookups.

    `Price` stays in the fingerprint, scoped to the same asset keys. It is the
    fallback source for an asset without enough warehouse history, so excluding
    it would serve a stale returns panel for exactly the assets it supports.

    Lazy import: portfolio -> marketdata is the allowed dependency direction
    (marketdata never imports portfolio's domain).
    """

    if asset_keys is None:
        symbols = None
    else:
        asset_keys = sorted(set(asset_keys))
        rows = Asset.objects.filter(key__in=asset_keys).values_list(
            "tse_symbol", "brs_symbol"
        )
        # Every panel appends cash USD, and Tether-quoted archive rows need
        # USDT/IRT even when neither rate asset is in the requested universe.
        symbols = sorted({symbol for row in rows for symbol in row if symbol}
                         | {"USD", "USDT_IRT"})

    def _max_id(qs, *, symbol_keyed=True):
        if symbol_keyed and symbols is not None:
            # Grouping disables PostgreSQL's min/max shortcut, which otherwise
            # walks the global id index and filters unrelated archive rows.
            # The symbol/id indexes keep each group index-served instead.
            return (
                qs.filter(symbol__in=symbols)
                .values("symbol")
                .annotate(max_id=Max("id"))
                .aggregate(version=Max("max_id"))["version"]
                or 0
            )
        return qs.aggregate(version=Max("id"))["version"] or 0

    price_rows = Price.objects
    if asset_keys is not None:
        price_rows = price_rows.filter(asset__key__in=asset_keys)

    return "{}:{}:{}:{}:{}:{}:{}:{}".format(
        hex(_max_id(price_rows, symbol_keyed=False))[2:] if include_price else "-",
        hex(_max_id(DailyStockHistory.objects))[2:],
        hex(_max_id(GoldCurrencyHistory.objects))[2:],
        hex(_max_id(MarketCandle.objects))[2:],
        hex(_max_id(MarketDailyBar.objects))[2:],
        hex(_max_id(RejectedRecord.objects))[2:],
        hex(_max_id(SymbolIntegrity.objects))[2:],
        hex(_max_id(InstrumentListingHistory.objects))[2:],
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


def _trading_session_index(start: dt.datetime, end: dt.datetime) -> pd.DatetimeIndex:
    """Gregorian index of the days the TSE actually traded, from the warehouse.

    Shares `actual_trading_days()` with `marketdata.integrity` so the gap gate
    here and the nightly integrity gate agree on what a missing session is.
    """
    from marketdata.calendars import actual_trading_days

    days = actual_trading_days(start=to_jalali_str(start), end=to_jalali_str(end))
    if not days:
        return pd.DatetimeIndex([])
    index = _jalali_to_gregorian_index(pd.Series(sorted(days)))
    return index[index.notna()].sort_values()


def _align_to_trading_sessions(panel: pd.DataFrame) -> pd.DataFrame:
    """Resample a mixed panel onto the TSE session calendar.

    Gold, FX and crypto quote seven days a week; TSE stocks trade five. On a
    plain calendar index every weekend and holiday reads as missing data for
    every stock, so a gap test counted in calendar days disqualifies the entire
    exchange. Sampling the always-on series on trading days instead is lossless
    for them and makes the two kinds of column directly comparable.
    """
    if panel.empty:
        return panel
    sessions = _trading_session_index(panel.index.min(), panel.index.max())
    if sessions.empty:
        return panel
    return panel.reindex(panel.index.intersection(sessions))


def _closure_explained(left: pd.Timestamp, right: pd.Timestamp) -> bool:
    """True when every missing session between two dates was an exchange closure.

    A warehouse hole and a shut exchange look identical from the session index
    alone, because that index is derived from the same rows that are missing.
    They are told apart by trading activity: on a closed day the provider still
    emits a row per symbol carrying the previous price with zero volume and zero
    trades, so the market-wide totals are zero.
    """

    closures = market_closure_days(
        start=to_jalali_str(left), end=to_jalali_str(right)
    )
    if not closures:
        return False
    # Every calendar day strictly between the two observed sessions must be
    # either a closure day or a non-session (weekend/holiday with no row at
    # all). A single genuinely-missing trading day means this is an ingest hole.
    missing = _trading_session_index(
        left + pd.Timedelta(days=1), right - pd.Timedelta(days=1)
    )
    return all(to_jalali_str(day) in closures for day in missing)


def _trim_to_contiguous(panel: pd.DataFrame) -> pd.DataFrame:
    """Drop everything before the most recent ingest outage.

    A hole in the warehouse leaves no trace in the index once the panel is on
    the session calendar, because that calendar is built from the same rows.
    Splicing across it would turn three missing months into one enormous daily
    return, so the window starts after the break instead.

    An exchange closure is NOT such a hole: no data is missing, the market was
    shut. Trimming there discarded a decade of history over the 83-day 1404-1405
    closure, which is why every lookback window used to return the same ~55
    sessions. Closure-explained breaks are kept; `_mask_closure_returns` removes
    the distorted return each asset carries across them -- per asset, because
    they do not all resume on the day the exchange does.
    """
    index = panel.index
    if len(index) < 2:
        return panel
    spans = (index[1:] - index[:-1]).days
    breaks = [
        i for i, days in enumerate(spans)
        if days > MAX_OUTAGE_CALENDAR_DAYS
        and not _closure_explained(index[i], index[i + 1])
    ]
    if not breaks:
        return panel
    return panel.iloc[breaks[-1] + 1:]


def _mask_closure_returns(returns: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """NaN each return that spans a long break in *its own* asset's history.

    Reopening after 83 shut days produces one row holding nearly three months of
    price movement. It is a real move, but it is not a daily return, and feeding
    it to an annualized volatility or a covariance estimate corrupts both. The
    history either side stays; only the bridging observation is dropped.

    Measured per column, not on the panel index, because assets do not all
    resume on the day the exchange does. Read from the index, the war closure is
    a single 83-day jump from 2026-02-25 to 2026-05-19, and masking that one row
    is right for everything that reopened with the market. `کاما` did not: its
    first post-halt price is 2026-05-24, five sessions later. Its 88-day move
    therefore lands on a date the index-wide mask never looks at, and shipped as
    a +39.6% single-day return with 54.8% of the book behind it.

    Per column the span is measured between an asset's own consecutive
    observations, so the mask follows each asset to whatever day it actually
    resumed. A market-wide closure still works: it is simply the case where
    every column broke at once.

    The prices themselves are untouched and correct; only the claim that this
    move happened in one day is withdrawn.
    """
    if returns.empty or panel.empty:
        return returns
    for key in returns.columns:
        if key not in panel.columns:
            continue
        observed = panel.index[panel[key].notna()]
        if len(observed) < 2:
            continue
        spans = (observed[1:] - observed[:-1]).days
        # Scalar sets, deliberately. A closure leaves ~one breach per column, so
        # batching them per column adds an index intersection to save a single
        # assignment: measured at 1000 instruments that is 218 ms against 178 ms.
        for day in observed[1:][spans > MAX_OUTAGE_CALENDAR_DAYS]:
            if day in returns.index:
                returns.loc[day, key] = np.nan
    return returns


def normalize_as_of(as_of) -> dt.datetime | None:
    """Normalize as_of to a timezone-aware datetime."""
    if as_of is None:
        return None
    if isinstance(as_of, str):
        try:
            if "T" in as_of:
                parsed = dt.datetime.fromisoformat(as_of)
                return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed
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
    held_keys: frozenset[str] = frozenset(),
) -> list[dict]:
    """Resolve universe items to dicts with key, symbol, and source.

    Each item in resolved list has:
      * 'key': column key to use in the DataFrame (e.g. 'kama_stock' or symbol)
      * 'symbol': the warehouse symbol ('کاما', 'USD', etc.)
      * 'source': 'tse' or 'brs'
      * 'asset': Asset object if exists
      * 'proxied_from': the asset key whose series was borrowed, when this asset
        has no provider symbol of its own (set only for `held_keys`)

    `held_keys` enables proxy resolution: a manual asset with no symbol but with
    `Asset.proxy_key` set borrows the proxy's symbol, so a Swiss gold bar is
    measured against gold instead of against the handful of live ticks its manual
    valuation produced. This is deliberately opt-in -- resolving proxies for the
    optimizer's candidate universe would hand it two identical columns to choose
    between, which is a singular covariance and an arbitrary allocation.
    """

    resolved = []
    assets = {a.key: a for a in Asset.objects.filter(is_active=True).exclude(is_house=True)}
    asset_by_symbol = {}
    for a in assets.values():
        sym = a.tse_symbol or a.brs_symbol
        if sym:
            asset_by_symbol[sym] = a

    def _entry(a: Asset) -> dict:
        """Universe entry for a catalog asset, following its proxy when held."""
        symbol = a.tse_symbol or a.brs_symbol or ""
        if symbol:
            return {
                "key": a.key,
                "symbol": symbol,
                "source": "tse" if a.tse_symbol else "brs",
                "asset": a,
            }
        proxy = assets.get(a.proxy_key) if a.key in held_keys else None
        proxy_symbol = (proxy.tse_symbol or proxy.brs_symbol or "") if proxy else ""
        if not proxy_symbol:
            return {"key": a.key, "symbol": "", "source": "brs", "asset": a}
        return {
            "key": a.key,
            "symbol": proxy_symbol,
            "source": "tse" if proxy.tse_symbol else "brs",
            "asset": a,
            "proxied_from": proxy.key,
        }

    if universe is None:
        for a in assets.values():
            resolved.append(_entry(a))
    else:
        # One query for every symbol that is not already a catalog asset,
        # instead of one (or two) per symbol inside the loop below. The
        # market-wide universe is ~1,900 symbols, and `daily_returns_matrix` is
        # the single function all the analytics surfaces route through, so the
        # per-item version issued up to ~3,800 queries for one request. The
        # InstrumentListingHistory lookup further down was already batched with
        # `symbol__in`; this branch simply had not been.
        unknown = [item for item in universe
                   if item not in assets and item not in asset_by_symbol]
        # A single case-insensitive fetch serves both lookups below. The
        # case-insensitive result set is a superset of the exact one -- anything
        # matching `symbol=X` also matches `LOWER(symbol)=LOWER(X)` -- so both
        # maps are built from it, and the exact map is still consulted first so
        # priority between the two branches is unchanged.
        instruments, instruments_ci = {}, {}
        if unknown:
            from django.db.models.functions import Lower

            for mi in MarketInstrument.objects.annotate(
                _lowered=Lower("symbol")
            ).filter(_lowered__in={item.lower() for item in unknown}):
                instruments.setdefault(mi.symbol, mi)
                instruments_ci.setdefault(mi._lowered, mi)

        for item in universe:
            if item in assets:
                resolved.append(_entry(assets[item]))
            elif item in asset_by_symbol:
                a = asset_by_symbol[item]
                resolved.append({
                    "key": a.key,
                    "symbol": item,
                    "source": "tse" if a.tse_symbol else "brs",
                    "asset": a
                })
            else:
                mi = instruments.get(item)
                if mi:
                    resolved.append({
                        "key": item,
                        "symbol": item,
                        "source": "tse" if mi.source == MarketInstrument.Source.TSETMC else "brs",
                        "asset": None
                    })
                else:
                    mi = instruments_ci.get(item.lower())
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
    from portfolio.models import Holding, Asset

    active_assets = list(Asset.objects.filter(is_active=True).exclude(is_house=True).values_list("key", flat=True))

    if mode == "held":
        # `is_hidden` holdings are owned but deliberately not counted, so they are
        # not part of "what I hold" for risk purposes either.
        res = []
        if account is not None:
            res = list(
                account.holdings.filter(is_hidden=False)
                .values_list("asset__key", flat=True)
            )
        elif user is not None:
            res = list(
                Holding.objects.filter(account__user=user, is_hidden=False)
                .values_list("asset__key", flat=True)
            )

        if not res:
            return active_assets
        return res

    elif mode == "market":
        from marketdata.models import MarketInstrument
        if not MarketInstrument.objects.filter(eligible=True).exists():
            return active_assets

        from marketdata.universe import get_candidate_universe
        candidates, _ = get_candidate_universe()
        return candidates

    return None


def _live_keys_memo_key(history_days, as_of_dt, universe, basis, held_keys) -> str:
    # Per Tehran day: the window slides daily, and with it which assets have
    # enough warehouse history to skip the live fallback.
    day = timezone.now().astimezone(_TEHRAN).date().isoformat()
    base = _returns_cache_key(history_days, as_of_dt, universe, basis, "-", held_keys)
    return f"returns:live-keys:v1:{day}:{base}"


def _returns_version(asset_keys, live_keys) -> str:
    """Cache version for one returns matrix: exactly the rows it reads.

    The warehouse half is `_price_version_fingerprint` without its `Price`
    component. `Price` -- the live 20-second ticks -- only enters a matrix for
    the assets `_load_price_panel` sends to the live fallback (no or too little
    warehouse history), so it is versioned over THOSE assets alone, as recorded
    by the previous build of the same matrix (`live_keys`). Versioning it over
    every asset in the panel, as before, rotated the key on every tick of every
    held stock while the market was open, although for a stock with a warehouse
    series no tick changes a single number in the matrix; every risk, frontier
    and optimization request rebuilt years of history from scratch for nothing.

    With no record yet (first build of the day, or evicted) the whole panel's
    `Price` rows are versioned, as before -- the conservative answer.
    """
    warehouse = _price_version_fingerprint(asset_keys, include_price=False)
    if live_keys is None:
        price_rows = Price.objects
        if asset_keys is not None:
            price_rows = price_rows.filter(asset__key__in=asset_keys)
        tag = "a"
    else:
        price_rows = Price.objects.filter(asset__key__in=live_keys)
        tag = "l"
    latest = (
        price_rows.aggregate(version=Max("id"))["version"] or 0
        if live_keys is None or live_keys else 0
    )
    return f"{tag}{latest:x}:{warehouse}"


def _returns_cache_key(
    history_days: int,
    as_of: dt.datetime | None,
    universe: list[str] | None,
    basis: str,
    version: str,
    held_keys: frozenset[str] = frozenset(),
) -> str:
    basis = normalize_basis(basis)
    if universe is None:
        univ_str = "default"
    else:
        sorted_univ = sorted(universe)
        univ_str = hashlib.md5(
            ",".join(sorted_univ).encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:16]

    # held_keys changes which columns survive the gates, so it must version the
    # cache -- otherwise the optimizer's strict matrix and the risk card's
    # relaxed one collide on the same key.
    if held_keys:
        held_str = hashlib.md5(
            ",".join(sorted(held_keys)).encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:16]
    else:
        held_str = "none"

    as_of_str = "latest" if as_of is None else as_of.date().isoformat()
    return (
        f"returns:daily:{history_days}d:as_of:{as_of_str}:univ:{univ_str}"
        f":basis:{basis}:held:{held_str}:v{version}"
    )



def _load_price_panel(
    history_days: int,
    as_of: dt.datetime | None = None,
    universe: list[str] | None = None,
    held_keys: frozenset[str] = frozenset(),
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    """Per-asset daily close panel: warehouse series preferred, Price fallback.

    Columns are asset keys, indexed by (Gregorian) date. NaN where an asset had
    no row that day.

    Returns `(panel, excluded, warnings)`. Keys in `held_keys` are never dropped
    by the `SymbolIntegrity` gate: that gate screens ~1000 instruments for
    *investability* over a fixed trailing 180-day window, which is the wrong
    question to ask about something the user already owns. For a held asset the
    verdict is downgraded to a warning and the column is kept, so the risk card
    reports a number with a caveat instead of reporting nothing.
    """
    as_of_dt = normalize_as_of(as_of)
    if as_of_dt is not None:
        cutoff = as_of_dt - dt.timedelta(days=history_days + _HISTORY_BUFFER_DAYS)
    else:
        cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(
            days=history_days + _HISTORY_BUFFER_DAYS
        )

    # A current nightly assessment must not leak into a historical cutoff. Its
    # window may contain observations that did not exist at that cutoff; the
    # bounded panel checks below are the point-in-time integrity gate instead.
    failed_symbols = {} if as_of_dt is not None else {
        si.symbol: si.reason for si in SymbolIntegrity.objects.filter(passes_gate=False)
    }

    # Resolve universe items
    resolved_univ = resolve_universe(universe, as_of=as_of_dt, held_keys=held_keys)

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

    # Bulk query MarketCandle (TSE)
    tse_rows = []
    if tse_symbols:
        qs_tse = candle_close_qs(tse_symbols, as_of=as_of_jalali)
        # Raw Rial -> Toman: the panel mixes TSE and BRS columns and is later
        # multiplied by a Toman FX rate, so units must agree before that.
        # `-id` is load-bearing, not cosmetic. Duplicate (symbol, date) candles
        # are collapsed downstream by `groupby(...).last()`, so without a total
        # ordering the surviving close is whatever Postgres returned first --
        # production carried 380,330 duplicate pairs that disagreed on price,
        # making every risk and return figure non-reproducible between runs.
        # Migration 0005 removes those and the unique constraint now forbids
        # them; this keeps the read deterministic regardless.
        tse_rows = [
            (sym, dt, tse_close_to_toman(close))
            for sym, dt, close in qs_tse.order_by(
                "symbol", "date_time", "-id"
            ).values_list("symbol", "date_time", "close_price")
        ]

        # ETF NAV has no MarketCandle rows -- Tsetmc/Nav.php is a live-only
        # endpoint (endpoints.py Nature.LIVE) with no historical form. It's
        # catalogued source=TSETMC (same l18-param convention as ordinary
        # stocks, see catalog.py's IRT-ISIN classification), so it belongs in
        # THIS block, not the BRS one below -- ETF rows never carry
        # source=BRS. MarketDailyBar.close_price is its real close (distilled
        # from live snapshots: open=first snapshot, close=last, not an
        # average -- marketdata.ingest.aggregate_market_daily_bars), stored
        # Rial per ingest_etf_nav_snapshot's documented policy, so it needs
        # the same tse_close_to_toman() conversion as every other row here.
        #
        # Crypto and commodity have the identical no-history problem and are
        # handled below, in the BRS block they belong to. They were left out
        # for a long time because a MarketDailyBar carries no unit of its own
        # and guessing currency from magnitude is something this project never
        # does -- but the unit was never actually lost: `ingest_market_snapshots`
        # stores the whole provider row, unit string included, and
        # `provenance.daily_bar_units` reads it back.
        etf_symbols = list(
            MarketInstrument.objects.filter(
                source=MarketInstrument.Source.TSETMC,
                symbol__in=tse_symbols,
                category=MarketInstrument.Category.ETF,
            ).values_list("symbol", flat=True)
        )
        if etf_symbols:
            qs_etf_bars = MarketDailyBar.objects.filter(
                asset_class=MarketDailyBar.AssetClass.ETF_NAV,
                symbol__in=etf_symbols,
                close_price__gt=0,
            )
            if as_of_jalali is not None:
                qs_etf_bars = qs_etf_bars.filter(date__lte=as_of_jalali)
            tse_rows.extend(
                (sym, date, tse_close_to_toman(close))
                for sym, date, close in qs_etf_bars.order_by("symbol", "date").values_list(
                    "symbol", "date", "close_price"
                )
            )

    # Bulk query GoldCurrencyHistory (BRS)
    brs_rows = []
    if brs_symbols:
        from marketdata.currency import to_toman
        from marketdata.provenance import (
            daily_bar_price, toman_rate_kwargs, toman_rate_tables,
        )
        qs_brs = GoldCurrencyHistory.objects.filter(
            symbol__in=brs_symbols,
            close_price__gt=0,
        )
        if as_of_jalali is not None:
            qs_brs = qs_brs.filter(date__lte=as_of_jalali)
        raw_brs_rows = list(qs_brs.order_by("symbol", "date").values_list(
            "symbol", "date", "close_price", "unit",
        ))
        cash_rates, tether_rates = toman_rate_tables(
            [unit for _sym, _day, _close, unit in raw_brs_rows],
            [day for _sym, day, _close, _unit in raw_brs_rows],
        )
        known_foreign_symbols = {
            item["symbol"] for item in resolved_univ
            if item["key"] in USD_QUOTED_KEYS
            or getattr(item.get("asset"), "asset_class", None) == Asset.AssetClass.CRYPTO
        }
        known_foreign_symbols.update(
            MarketInstrument.objects.filter(
                source=MarketInstrument.Source.BRS,
                symbol__in=brs_symbols,
                category__in=(
                    MarketInstrument.Category.CRYPTO,
                    MarketInstrument.Category.COMMODITY,
                ),
            ).values_list("symbol", flat=True)
        )
        for symbol, day, close, unit in raw_brs_rows:
            if not unit and symbol in known_foreign_symbols:
                continue
            value = to_toman(
                symbol, close, unit,
                **toman_rate_kwargs(unit, day, cash_rates, tether_rates),
            )
            if value > 0:
                brs_rows.append((symbol, day, value))
        # The live-only daily bars are already converted by their declared
        # snapshot unit. Both sources now reach this panel in Toman, so the
        # join cannot splice BTC Tether and Toman into one return series.

        covered = {(symbol, date) for symbol, date, _close in brs_rows}
        brs_assets = [
            item["asset"] for item in resolved_univ
            if item.get("asset") and item["source"] == "brs"
        ]
        brs_rows.extend(
            row for row in daily_bar_price(brs_assets, as_of=as_of_jalali)
            if (row[0], row[1]) not in covered
        )

    cutoff_jalali = to_jalali_str(cutoff)
    rejections = rejected_pairs(
        tse_symbols + brs_symbols, PRICE_SERIES_ENDPOINTS, since=cutoff_jalali
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
    short_warehouse_cols: dict[str, pd.Series] = {}
    fallback_keys: list[str] = []
    gate_excluded = []
    warnings: list[dict] = []

    from marketdata.calendars import market_for_asset, sessions_between

    univ_keys = [item["key"] for item in resolved_univ]
    asset_map = {a.key: a for a in Asset.objects.filter(key__in=univ_keys)}

    for item in resolved_univ:
        key = item["key"]
        symbol = item["symbol"]
        source = item["source"]

        if item.get("proxied_from"):
            warnings.append({
                "key": key,
                "reason": "proxied",
                "detail": item["proxied_from"],
            })

        # Check data integrity gate
        if symbol in failed_symbols:
            record = {
                "key": key,
                "reason": "integrity_gate_failed",
                "detail": failed_symbols[symbol],
            }
            if key not in held_keys:
                gate_excluded.append(record)
                continue
            warnings.append(record)

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

        # Staleness / survivorship guard: check if asset stopped trading near as_of
        if as_of_dt is not None and not series.index.empty:
            max_date = series.index.max()
            last_jalali = to_jalali_str(max_date)
            as_of_jalali = to_jalali_str(as_of_dt)
            asset_obj = asset_map.get(key)
            market = market_for_asset(asset_obj) if asset_obj else "tse"
            stale_sessions = sessions_between(last_jalali, as_of_jalali, market=market)
            if stale_sessions > MAX_FORWARD_FILL_SESSIONS:
                gate_excluded.append({
                    "key": key,
                    "reason": "survivorship_guard_failed",
                    "stale_sessions": stale_sessions,
                    "max_forward_fill_sessions": MAX_FORWARD_FILL_SESSIONS,
                    "detail": f"Series is {stale_sessions} sessions stale at as_of {as_of_dt.date()}",
                })
                continue

        if len(series) < MIN_DAILY_RETURNS:
            fallback_keys.append(key)
            # A held asset must not lose its only history to a threshold whose
            # job is picking optimizer candidates. Keep the short series aside
            # and use it below if the live-tick fallback turns up nothing better.
            if key in held_keys and not series.empty:
                short_warehouse_cols[key] = series.groupby(series.index).last()
            continue

        series = series.groupby(series.index).last()
        warehouse_cols[key] = series

    fallback_panel = _load_live_price_panel(cutoff, as_of_dt, fallback_keys)
    for key in fallback_keys:
        if key in fallback_panel.columns:
            continue
        if key in short_warehouse_cols:
            warehouse_cols[key] = short_warehouse_cols[key]
            warnings.append({
                "key": key,
                "reason": "short_history",
                "detail": f"{len(short_warehouse_cols[key])} warehouse observations",
            })
            continue
        gate_excluded.append({
            "key": key,
            "reason": "no_price_history",
            "detail": "No trustworthy price observations in the requested window",
        })

    tse_keys = {item["key"] for item in resolved_univ if item["source"] == "tse"}
    if not warehouse_cols:
        panel = fallback_panel
    else:
        panel = pd.DataFrame(warehouse_cols)
        panel.index = panel.index.normalize()
        if not fallback_panel.empty:
            panel = panel.join(fallback_panel, how="outer")
        panel = panel.sort_index()
    # Only worth doing when a five-day-a-week column is in play; a gold-only
    # panel is legitimately daily and should keep its weekend observations.
    if tse_keys.intersection(panel.columns):
        panel = _align_to_trading_sessions(panel)
    panel = _trim_to_contiguous(panel)
    # Which assets this panel read from the live Price table (whether or not it
    # found usable rows there): `_returns_version` keys the cache on exactly these.
    panel.attrs["live_fallback_keys"] = sorted(fallback_keys)
    return panel, gate_excluded, warnings


def _load_live_price_panel(cutoff: dt.datetime, as_of: dt.datetime | None, keys: list[str]) -> pd.DataFrame:
    """The original Price-table loader, restricted to the given asset keys and cutoff/as_of.

    Reached for two cases: an asset with genuinely no provider symbol at all
    (bitcoin_usd, swiss_gold_bar_*), and a warehouse/ETF-NAV-backed asset whose
    warehouse series is too shallow yet (see `_load_price_panel`'s
    MIN_DAILY_RETURNS gate -- e.g. kama_stock right after being newly tracked).

    Each day is the median of that day's last up to 3 ticks: close enough to
    the close-to-close definition every warehouse-backed column in the panel
    uses (a full-day mean would smear an intraday move into a return that
    never happened), but resistant to one glitched final tick deciding the
    whole day's return on its own -- the exact bug a prior mean-based version
    of this function existed to fix (see `c199d17`), before the crypto/
    commodity/ETF-NAV warehouse gap this fallback used to cover was closed and
    the outlier-tick case became the dominant remaining risk here instead.
    """
    if not keys:
        return pd.DataFrame()

    foreign_seed = Q(asset__key__in=USD_QUOTED_KEYS)
    tse_asset = ~Q(asset__tse_symbol="")
    qs = Price.objects.filter(
        positive_price_q(), asset__is_active=True, asset__key__in=keys,
        fetched_at__gte=cutoff,
    ).filter(
        (~foreign_seed & ~tse_asset)
        | (foreign_seed & Q(price_unit=Price.Unit.IRT, price_unit_verified=True))
        | (~foreign_seed & tse_asset & Q(price_unit=Price.Unit.IRR, price_unit_verified=True)),
    ).exclude(asset__is_house=True)

    if as_of is not None:
        qs = qs.filter(fetched_at__lte=as_of)

    rows = (
        qs.select_related("asset")
        .order_by("asset__key", "fetched_at")
        .annotate(price=Coalesce(
            "price_iranian", "price_foreign",
            output_field=DecimalField(max_digits=20, decimal_places=4),
        ))
        .values("asset__key", "fetched_at", "price")
    )
    if not rows:
        return pd.DataFrame()

    rows = list(rows)

    # Exclude RejectedRecord matches
    from portfolio.models import Asset

    asset_rows = {a.key: a for a in Asset.objects.filter(key__in=keys)}
    assets = {
        key: asset.tse_symbol or asset.brs_symbol or ""
        for key, asset in asset_rows.items()
    }
    symbols = [s for s in assets.values() if s]

    rejections = rejected_pairs(
        symbols, PRICE_SERIES_ENDPOINTS, since=to_jalali_str(cutoff)
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
    df["day"] = df["fetched_at"].dt.normalize()

    daily = (
        df.sort_values("fetched_at")
        .groupby(["asset__key", "day"])["price"]
        .apply(lambda ticks: ticks.tail(3).median())
    )
    panel = daily.unstack("asset__key")
    for key, asset in asset_rows.items():
        if asset.tse_symbol and key not in USD_QUOTED_KEYS and key in panel.columns:
            panel[key] = panel[key].map(lambda value: float(tse_close_to_toman(value)))
    return panel


def _gap_profile(observed: np.ndarray) -> tuple[int, int]:
    """(leading_gap, interior_gap) for a boolean 'has an observation' array.

    The leading run of missing days -- everything before an asset's FIRST
    observation -- is not a gap in its history, it is the absence of history:
    the asset was listed, bought, or first tracked partway into the window.
    Counting it as a gap accused every recently-added holding of
    `price_gap_exceeded`, a data-corruption verdict, when nothing was wrong with
    the data. Only the interior runs describe a hole in a series that exists.
    Trailing runs stay in the interior count: a series that stops mid-window IS
    a hole (a stale or delisted asset), and forward-filling it is exactly what
    MAX_FORWARD_FILL_SESSIONS is there to bound.
    """
    present = np.flatnonzero(observed)
    if present.size == 0:
        return len(observed), 0
    leading = int(present[0])
    interior = current = 0
    for is_present in observed[leading:]:
        current = 0 if is_present else current + 1
        interior = max(interior, current)
    return leading, interior


def _build_returns_matrix(
    panel: pd.DataFrame,
    held_keys: frozenset[str] = frozenset(),
    as_of: dt.datetime | None = None,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    """Price panel -> (daily simple returns, excluded, warnings).

    Excludes any asset with fewer than MIN_DAILY_RETURNS non-NaN return rows.
    Gaps are counted in trading sessions, matching `marketdata.integrity`, and
    separately in calendar days to catch ingest outages that the session
    calendar cannot see because it is derived from the same warehouse.

    Keys in `held_keys` clear a lower bar. MIN_DAILY_RETURNS and MIN_COVERAGE
    decide whether an asset is a usable *optimizer candidate*; an asset the user
    already owns is reported on with whatever history it has (>= 2 returns) and
    the shortfall is returned as a warning instead. A real interior gap still
    excludes it either way -- forward-filling past MAX_FORWARD_FILL_SESSIONS
    invents prices, and a made-up return is worse than a missing one.
    """

    if panel.empty:
        return pd.DataFrame(), [], []

    filled = panel.ffill(limit=MAX_FORWARD_FILL_SESSIONS)
    returns = filled.pct_change(fill_method=None)
    # History either side of an exchange closure is kept (see _trim_to_contiguous),
    # so the one row bridging it holds months of movement. Drop just that row.
    returns = _mask_closure_returns(returns, panel)
    excluded: list[dict] = []
    warnings: list[dict] = []
    keep: list[str] = []

    from marketdata.calendars import market_for_asset, sessions_between
    from portfolio.models import Asset

    asset_map = {a.key: a for a in Asset.objects.filter(key__in=returns.columns)}
    ref_dt = as_of if as_of is not None else timezone.now()
    ref_jalali = to_jalali_str(ref_dt)

    for key in returns.columns:
        held = key in held_keys
        leading_gap, interior_gap = _gap_profile(panel[key].notna().to_numpy())
        non_nan = int(returns[key].notna().sum())
        expected = max(len(returns.index) - 1 - leading_gap, 0)
        coverage = non_nan / expected if expected else 0.0

        # Trailing gap against market calendar relative to as_of
        asset_obj = asset_map.get(key)
        market = market_for_asset(asset_obj) if asset_obj else "tse"
        max_date = panel[key].dropna().index.max()
        trailing_sessions = 0
        # `pd.isna`, not `is not None`. An empty index's `.max()` is pandas.NaT,
        # which is emphatically not None, and NaT's .year/.month/.day are all
        # float nan -- so the old check let it through and to_jalali_str died
        # inside jdatetime with "TypeError: 'float' object cannot be interpreted
        # as an integer". That reached the user as a 500 on MyOptimal whenever a
        # held asset had no observation anywhere in the window.
        #
        # Leaving trailing_sessions at 0 is deliberate rather than a fallback. A
        # column with nothing in it is 100% leading gap, and `_gap_profile`
        # defines a leading run as the absence of history rather than a hole in
        # it, so the honest verdict is `insufficient_history` (days: 0) from the
        # branch below -- not the `price_gap_exceeded` data-corruption verdict
        # that inventing a large trailing gap here would produce.
        if not pd.isna(max_date):
            last_jalali = to_jalali_str(max_date)
            trailing_sessions = sessions_between(last_jalali, ref_jalali, market=market)

        effective_gap = max(interior_gap, trailing_sessions)
        if effective_gap > MAX_FORWARD_FILL_SESSIONS:
            excluded.append({
                "key": key,
                "reason": "price_gap_exceeded",
                "max_gap_sessions": effective_gap,
            })
        elif non_nan < MIN_DAILY_RETURNS:
            record = {"key": key, "reason": "insufficient_history", "days": non_nan}
            if not held or non_nan < 2:
                excluded.append(record)
            else:
                warnings.append({**record, "reason": "short_history"})
                keep.append(key)
        elif coverage < MIN_COVERAGE:
            record = {
                "key": key,
                "reason": "insufficient_coverage",
                "observations": non_nan,
                "expected_sessions": expected,
                "coverage": coverage,
            }
            if held:
                warnings.append({**record, "reason": "low_coverage"})
                keep.append(key)
            else:
                excluded.append(record)
        else:
            keep.append(key)
            # Only a leading gap big enough to matter is worth a badge: nearly
            # every series starts a session or two into the window simply
            # because of where the cutoff falls.
            if leading_gap > MAX_FORWARD_FILL_SESSIONS:
                warnings.append({
                    "key": key,
                    "reason": "short_history",
                    "days": non_nan,
                    "detail": f"series starts {leading_gap} sessions into the window",
                })
    returns = returns[keep] if keep else pd.DataFrame(index=returns.index)
    return returns, excluded, warnings


def periods_per_year(index: pd.Index) -> float:
    """Observations per year implied by the index's average spacing.

    ~252 on the TSE session calendar (Sat-Wed), ~365 on an all-gold panel, which
    quotes seven days a week. Annualizing a 7-day series with a hardcoded 252
    overstates volatility by sqrt(365/252) ~= 1.20x and understates Sharpe by the
    same factor, so the figure has to come from the data rather than a constant.

    Uses the MEAN spacing, not the median: a Sat-Wed calendar spaces its
    observations 1,1,1,1,3 days apart, whose median is 1 and would report a
    five-day-a-week series as if it traded daily. Spacings longer than
    MAX_OUTAGE_CALENDAR_DAYS are dropped first -- an exchange closure is not the
    series' cadence, and the 83-day shutdown of 1404-1405 would otherwise drag a
    252-session year down to ~190.
    """
    if index is None or len(index) < 3:
        return float(TRADING_DAYS_PER_YEAR)
    gaps = np.diff(np.asarray(index, dtype="datetime64[D]")).astype(float)
    gaps = gaps[(gaps > 0) & (gaps <= MAX_OUTAGE_CALENDAR_DAYS)]
    if gaps.size == 0:
        return float(TRADING_DAYS_PER_YEAR)
    mean_spacing = float(gaps.mean())
    if not np.isfinite(mean_spacing) or mean_spacing <= 0:
        return float(TRADING_DAYS_PER_YEAR)
    return 365.25 / mean_spacing


def toman_price_panel(
    *, history_days: int, universe: list[str] | None = None,
    held_keys: frozenset[str] = frozenset(), as_of: dt.datetime | None = None,
    gate: bool = False,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    """Daily close panel in Toman. The supported reader for absolute prices.

    Archive rows are in Toman after unit-aware conversion. Live Price fallback
    rows require a verified unit and TSE Rial quotes are divided by ten.
    Callers that need the
    forward-fill bound should read `excluded` for `price_gap_exceeded`, which
    is decided in `_build_returns_matrix` against the same panel.
    """
    panel, excluded, warnings = _load_price_panel(
        history_days, as_of=as_of, universe=universe, held_keys=held_keys
    )
    if gate:
        _, gate_excluded, gate_warnings = _build_returns_matrix(
            panel, held_keys, as_of=as_of
        )
        excluded = [*excluded, *gate_excluded]
        warnings = [*warnings, *gate_warnings]
    return panel, excluded, warnings


def daily_returns_matrix(
    *,
    history_days: int = DEFAULT_HISTORY_DAYS,
    as_of=None,
    universe: list[str] | None = None,
    basis: str = "nominal_toman",
    held_keys: frozenset[str] = frozenset(),
) -> tuple[pd.DataFrame, list[dict]]:
    """Return `(daily_returns_df, excluded)` for the eligible universe.

    Cached per price-version fingerprint. On a cache miss, builds the matrix
    from one bounded Price query and stores it serialized under the versioned
    key (TTL 600s). The df is indexed by date, columns are asset keys, values
    are daily simple returns (float).

    `held_keys` marks assets the caller actually owns. They bypass the
    universe-screening gates (integrity, coverage, minimum history) and their
    shortfalls come back on `df.attrs["warnings"]` instead of removing the
    column -- see `_load_price_panel` and `_build_returns_matrix`. Default empty
    reproduces the strict screening every other caller relies on.

    `df.attrs` also carries `periods_per_year`, the panel's measured sampling
    frequency, which any annualizing consumer must use in place of a constant.
    """
    as_of_dt = normalize_as_of(as_of)
    basis = normalize_basis(basis)
    held_keys = frozenset(held_keys)
    asset_keys = _returns_asset_keys(universe, held_keys)
    live_memo_key = _live_keys_memo_key(history_days, as_of_dt, universe, basis, held_keys)
    live_keys = cache.get(live_memo_key)
    version = _returns_version(asset_keys, live_keys)
    key = _returns_cache_key(history_days, as_of_dt, universe, basis, version, held_keys)
    cached = cache.get(key)
    if cached is not None:
        df = pd.DataFrame(
            data=cached["data"],
            index=pd.to_datetime(cached["index"], utc=True),
            columns=cached["columns"],
        )
        df.attrs["warnings"] = cached.get("warnings", [])
        df.attrs["periods_per_year"] = cached.get(
            "periods_per_year", float(TRADING_DAYS_PER_YEAR)
        )
        return df, cached["excluded"]

    panel, gate_excluded, panel_warnings = toman_price_panel(
        history_days=history_days, as_of=as_of_dt, universe=universe,
        held_keys=held_keys,
    )
    # Union, never replace: a key that left the fallback since an earlier build
    # stays versioned, which can only cost a spare rebuild, never a stale hit.
    read_live = sorted(set(live_keys or ()) | set(panel.attrs.get("live_fallback_keys", ())))

    # Apply basis conversion. real_toman raises deflator.CpiUnavailable (see
    # config/settings.py) when the window reaches a Jalali year with no
    # configured CPI — that must propagate, not fall back to nominal, so a
    # caller never mistakes an unpriced basis for a real number.
    if basis == "usd_denominated" and "usd_cash" in panel.columns:
        usd_series = panel["usd_cash"]
        for col in panel.columns:
            panel[col] = to_basis(panel[col], basis, usd_series=usd_series)
    elif basis == "usdt_denominated":
        usdt_series = panel.get("usdt_irt")
        # A missing Tether series is a gap, never permission to substitute
        # cash USD. `to_basis` loads USDT_IRT directly when it is absent here.
        for col in panel.columns:
            panel[col] = to_basis(panel[col], basis, usd_series=usdt_series)
    elif basis == "real_toman":
        for col in panel.columns:
            panel[col] = to_basis(panel[col], basis)

    # Frequency is a property of the price panel, not of the surviving columns:
    # measure it before the gates can thin the index.
    frequency = periods_per_year(panel.index)

    returns, excluded, matrix_warnings = _build_returns_matrix(
        panel, held_keys, as_of=as_of_dt
    )
    excluded.extend(gate_excluded)
    warnings = panel_warnings + matrix_warnings

    # Filter columns to only include the requested universe
    resolved_univ = resolve_universe(universe, held_keys=held_keys)
    requested_keys = [item["key"] for item in resolved_univ]
    keep = [k for k in requested_keys if k in returns.columns]
    returns = returns[keep] if keep else pd.DataFrame(index=returns.index)

    # Filter excluded/warning lists to only include requested universe keys
    requested_keys_set = set(requested_keys)
    excluded = [e for e in excluded if e.get("key") in requested_keys_set]
    warnings = [w for w in warnings if w.get("key") in requested_keys_set]

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
    returns.attrs["warnings"] = warnings
    returns.attrs["periods_per_year"] = frequency
    entry = {
        **payload,
        "excluded": excluded,
        "warnings": warnings,
        "periods_per_year": frequency,
    }
    cache.set(key, entry, timeout=RETURNS_CACHE_TTL)
    # The next build of this matrix versions on the assets this one read live.
    # (It costs one rebuild after the first build of a day; filing this result
    # under that next version too is not exact -- that version is read after
    # the build, so a tick landing mid-build would be filed as already seen.)
    cache.set(live_memo_key, read_live, timeout=24 * 3600)
    return returns, excluded


def correlation_matrix(
    *,
    history_days: int = DEFAULT_HISTORY_DAYS,
    as_of=None,
    universe: list[str] | None = None,
    basis: str = "nominal_toman",
    returns_df=None,
) -> dict:
    """Correlation payload for the eligible universe from the returns df.

    NaN correlations (assets with no overlap) become 0 so the matrix is dense
    and JSON-serializable.

    `returns_df` lets a caller that has already built the panel -- notably
    `portfolio_diagnostics`, which builds it with its own `held_keys` gating --
    reuse that exact matrix instead of paying for a second
    `daily_returns_matrix` pass whose different arguments would also miss the
    cache and could disagree about which assets are eligible.
    """
    df = returns_df
    if df is None:
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

import datetime as dt
from decimal import Decimal
import numpy as np
import pandas as pd
from django.conf import settings
from django.db.models import Count
from django.utils import timezone
import jdatetime

from marketdata.calendars import candle_close_qs
from marketdata.currency import tse_close_to_toman
from django.core.cache import cache

from marketdata.models import (
    GoldCurrencyHistory,
    MarketCandle,
    MarketInstrument,
    SymbolIntegrity,
)
from portfolio.services.returns import normalize_as_of, to_jalali_str

# Tunable thresholds
MIN_MEDIAN_DAILY_VOLUME = float(getattr(settings, "MIN_MEDIAN_DAILY_VOLUME", 1000))
MIN_MEDIAN_DAILY_TURNOVER_TOMANS = float(getattr(settings, "MIN_MEDIAN_DAILY_TURNOVER_TOMANS", 50000000))
MIN_DAILY_RETURNS = 30
# Capped small on purpose: the optimizer's covariance is only well-conditioned
# when the shared observation window is a large multiple of the asset count
# (see optimization.MIN_OBSERVATIONS_PER_ASSET). ~50 deep-history names
# sharing ~2000+ sessions gives a 40x ratio; 200 shallow-liquidity names gave
# a 45-row shared window and a Sharpe of 16.
MAX_UNIVERSE_SIZE = 50

UNIVERSE_CACHE_TTL = 600


def _universe_fingerprint() -> str:
    """Monotonic fingerprint of the four tables this screen reads.

    Same idea as the returns matrix's price-version key: a new candle ingest, an
    integrity-gate flip, or a change in provider eligibility must rotate the
    cache rather than wait out a TTL. Deliberately NOT the returns fingerprint --
    this screen never reads Price, so a live tick should not invalidate it.
    """
    def _max_id(model):
        return model.objects.order_by("-id").values_list("id", flat=True).first() or 0

    return "{}:{}:{}:{}".format(
        _max_id(MarketInstrument),
        _max_id(SymbolIntegrity),
        _max_id(MarketCandle),
        _max_id(GoldCurrencyHistory),
    )


def get_candidate_universe(
    as_of=None,
    history_days: int = 180,
    max_size: int = MAX_UNIVERSE_SIZE,
) -> tuple[list[str], list[dict]]:
    """Cached wrapper over the market screen.

    The screen scans every eligible instrument's full candle history and took
    ~27s on production data. Four call sites pay that -- BestOverallView, the
    `market` universe mode, the nightly best-overall task, and the diversifier
    ranking -- and it is a market-wide screen whose inputs change when the
    warehouse does, not per request.
    """
    key = "universe:candidates:{}:{}:{}:v{}".format(
        to_jalali_str(normalize_as_of(as_of) or timezone.now()),
        history_days,
        max_size,
        _universe_fingerprint(),
    )
    cached = cache.get(key)
    if cached is not None:
        return cached
    result = _compute_candidate_universe(
        as_of=as_of, history_days=history_days, max_size=max_size
    )
    cache.set(key, result, timeout=UNIVERSE_CACHE_TTL)
    return result


def _compute_candidate_universe(
    as_of=None,
    history_days: int = 180,
    max_size: int = MAX_UNIVERSE_SIZE,
) -> tuple[list[str], list[dict]]:
    """Determine the qualified universe of candidate symbol keys for optimization.

    Returns:
        (list_of_keys, list_of_excluded_dicts)
    """
    as_of_dt = normalize_as_of(as_of)
    if as_of_dt is None:
        as_of_dt = timezone.now()

    # Timeframe boundaries
    cutoff_dt = as_of_dt - dt.timedelta(days=history_days + 7) # Include buffer
    as_of_jalali = to_jalali_str(as_of_dt)
    cutoff_jalali = to_jalali_str(cutoff_dt)

    # 1. Load all eligible market instruments
    instruments = {mi.symbol: mi for mi in MarketInstrument.objects.filter(eligible=True)}
    if not instruments:
        return [], []

    # 2. Load integrity gates
    integrity_gates = {si.symbol: si for si in SymbolIntegrity.objects.all()}

    # 3. Query all stock candles in bulk to calculate liquidity and survivorship
    tse_symbols = [sym for sym, mi in instruments.items() if mi.source == MarketInstrument.Source.TSETMC]

    # Full-history depth per symbol (unbounded by the liquidity window above):
    # this is what lets a 50-name, multi-year universe stay well-conditioned.
    # `candle_close_qs` with no `as_of` returns every adjusted session on file.
    tse_depth: dict[str, int] = {}
    if tse_symbols:
        tse_depth = dict(
            candle_close_qs(tse_symbols)
            .values("symbol")
            .annotate(n=Count("id"))
            .values_list("symbol", "n")
        )

    # We query candles for active candidates
    candles_qs = candle_close_qs(tse_symbols, as_of=as_of_jalali).filter(
        date_time__gte=cutoff_jalali,
    ).order_by("symbol", "date_time")

    candles_data = {}
    for sym, dt_str, close, vol in candles_qs.values_list("symbol", "date_time", "close_price", "volume"):
        # Raw Rial -> Toman: turnover below is screened against
        # MIN_MEDIAN_DAILY_TURNOVER_TOMANS, an absolute Toman threshold.
        candles_data.setdefault(sym, []).append(
            (dt_str, float(tse_close_to_toman(close)), float(vol))
        )

    # Query all gold/currency histories in bulk for BRS
    brs_symbols = [sym for sym, mi in instruments.items() if mi.source == MarketInstrument.Source.BRS]
    brs_depth: dict[str, int] = {}
    if brs_symbols:
        brs_depth = dict(
            GoldCurrencyHistory.objects.filter(symbol__in=brs_symbols, close_price__gt=0)
            .values("symbol")
            .annotate(n=Count("id"))
            .values_list("symbol", "n")
        )
    brs_qs = GoldCurrencyHistory.objects.filter(
        symbol__in=brs_symbols,
        date__gte=cutoff_jalali,
        date__lte=as_of_jalali
    ).order_by("symbol", "date")

    brs_data = {}
    for sym, d_str, close in brs_qs.values_list("symbol", "date", "close_price"):
        brs_data.setdefault(sym, []).append((d_str, float(close)))

    candidates = []
    excluded = []

    # Helper to convert Jalali to Gregorian date
    def to_greg(val):
        # Narrow on purpose. A bare `except:` also swallows KeyboardInterrupt
        # and SystemExit, so a Ctrl-C or a worker shutdown landing inside this
        # helper was absorbed and read as "unparseable date" -- the loop below
        # then carried on as though nothing had happened. The parse itself can
        # only fail these three ways: a non-numeric part, the wrong number of
        # parts, or a date Jalali has no such day for.
        try:
            y, m, d = (int(part) for part in str(val).split(" ")[0].split("-"))
            return jdatetime.date(y, m, d).togregorian()
        except (ValueError, TypeError):
            return None

    # Evaluate each instrument
    for sym, mi in instruments.items():
        # Check integrity gate
        gate = integrity_gates.get(sym)
        if not gate or not gate.passes_gate:
            reason = gate.reason if gate else "No integrity check record found"
            excluded.append({
                "key": sym,
                "reason": "integrity_gate_failed",
                "detail": reason
            })
            continue

        if mi.source == MarketInstrument.Source.TSETMC:
            rows = candles_data.get(sym, [])
            if not rows:
                excluded.append({
                    "key": sym,
                    "reason": "no_price_history",
                    "detail": f"No candles found in window [{cutoff_jalali}, {as_of_jalali}]"
                })
                continue

            # Liquidity calculations
            vols = [r[2] for r in rows]
            turnovers = [r[1] * r[2] for r in rows]
            med_vol = np.median(vols)
            med_turnover = np.median(turnovers)

            if med_vol < MIN_MEDIAN_DAILY_VOLUME or med_turnover < MIN_MEDIAN_DAILY_TURNOVER_TOMANS:
                excluded.append({
                    "key": sym,
                    "reason": "insufficient_liquidity",
                    "detail": f"Median vol={med_vol:.1f} (min {MIN_MEDIAN_DAILY_VOLUME}), Median turnover={med_turnover:.1f} (min {MIN_MEDIAN_DAILY_TURNOVER_TOMANS})"
                })
                continue

            # Survivorship & history length
            # Check latest date
            last_date_g = to_greg(rows[-1][0])
            if last_date_g is None or (as_of_dt.date() - last_date_g).days > 30:
                excluded.append({
                    "key": sym,
                    "reason": "survivorship_guard_failed",
                    "detail": f"Latest update was {rows[-1][0]} which is > 30 days before as_of"
                })
                continue

            if len(rows) < MIN_DAILY_RETURNS:
                excluded.append({
                    "key": sym,
                    "reason": "insufficient_history",
                    "detail": f"Has {len(rows)} days of returns (min {MIN_DAILY_RETURNS})"
                })
                continue

            # Store candidate metadata for ranking
            candidates.append({
                "key": sym,
                "score": med_turnover,
                "history_depth": tse_depth.get(sym, 0),
                "source": "tse"
            })

        else: # BRS
            rows = brs_data.get(sym, [])
            if not rows:
                excluded.append({
                    "key": sym,
                    "reason": "no_price_history",
                    "detail": f"No gold history found in window [{cutoff_jalali}, {as_of_jalali}]"
                })
                continue

            # Check latest date
            last_date_g = to_greg(rows[-1][0])
            if last_date_g is None or (as_of_dt.date() - last_date_g).days > 30:
                excluded.append({
                    "key": sym,
                    "reason": "survivorship_guard_failed",
                    "detail": f"Latest update was {rows[-1][0]} which is > 30 days before as_of"
                })
                continue

            if len(rows) < MIN_DAILY_RETURNS:
                excluded.append({
                    "key": sym,
                    "reason": "insufficient_history",
                    "detail": f"Has {len(rows)} days of returns (min {MIN_DAILY_RETURNS})"
                })
                continue

            # Macro assets like USD or Gold ounces are always prioritized, give them a high score
            candidates.append({
                "key": sym,
                "score": float("inf"),
                "history_depth": brs_depth.get(sym, 0),
                "source": "brs"
            })

    # Rank by history depth first -- the point of the cap is a universe that
    # shares a deep multi-year window, not just today's most liquid names --
    # falling back to liquidity/macro score as a tiebreaker within that.
    candidates.sort(key=lambda x: (x["history_depth"], x["score"]), reverse=True)

    selected_keys = [c["key"] for c in candidates[:max_size]]
    
    # Add capped ones to excluded
    for c in candidates[max_size:]:
        excluded.append({
            "key": c["key"],
            "reason": "capped_by_universe_limit",
            "detail": f"Universe cap of {max_size} exceeded"
        })

    return selected_keys, excluded

import datetime as dt
from decimal import Decimal
import numpy as np
import pandas as pd
from django.conf import settings
from django.utils import timezone
import jdatetime

from marketdata.models import MarketInstrument, SymbolIntegrity, MarketCandle, GoldCurrencyHistory
from portfolio.services.returns import normalize_as_of, to_jalali_str

# Tunable thresholds
MIN_MEDIAN_DAILY_VOLUME = float(getattr(settings, "MIN_MEDIAN_DAILY_VOLUME", 1000))
MIN_MEDIAN_DAILY_TURNOVER_TOMANS = float(getattr(settings, "MIN_MEDIAN_DAILY_TURNOVER_TOMANS", 50000000))
MIN_DAILY_RETURNS = 30
MAX_UNIVERSE_SIZE = 200

def get_candidate_universe(
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
    
    # We query candles for active candidates
    candles_qs = MarketCandle.objects.filter(
        symbol__in=tse_symbols,
        timeframe="1d_adj",
        date_time__gte=cutoff_jalali,
        date_time__lte=as_of_jalali,
        close_price__gt=0
    ).order_by("symbol", "date_time")

    candles_data = {}
    for sym, dt_str, close, vol in candles_qs.values_list("symbol", "date_time", "close_price", "volume"):
        candles_data.setdefault(sym, []).append((dt_str, float(close), float(vol)))

    # Query all gold/currency histories in bulk for BRS
    brs_symbols = [sym for sym, mi in instruments.items() if mi.source == MarketInstrument.Source.BRS]
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
        try:
            y, m, d = (int(part) for part in str(val).split("-"))
            return jdatetime.date(y, m, d).togregorian()
        except:
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
                "source": "brs"
            })

    # Sort candidates by score descending (high liquidity first)
    candidates.sort(key=lambda x: x["score"], reverse=True)

    selected_keys = [c["key"] for c in candidates[:max_size]]
    
    # Add capped ones to excluded
    for c in candidates[max_size:]:
        excluded.append({
            "key": c["key"],
            "reason": "capped_by_universe_limit",
            "detail": f"Universe cap of {max_size} exceeded"
        })

    return selected_keys, excluded

"""Nightly precompute for the "Best Possible Portfolio Overall" page.

Kept out of `portfolio/tasks.py` deliberately (a concurrent edit is in
progress on that file); wired into Celery directly from `config/celery.py`
instead of relying on app autodiscovery, same pattern as
`portfolio/services/maintenance.py`.

Runs the market-wide optimizer over every tracked asset (not just one user's
holdings) across four lookback windows, for both max-Sharpe and min-volatility.
Each request-time read (`BestOverallView`) is then a pure read of the latest
snapshot per (window_days, scenario) -- no solver call in the request path.
"""
import logging
from decimal import Decimal

from celery import shared_task

from portfolio.optimization_models import OptimizationSnapshot
from portfolio.services.optimization import (
    MixedUnitUniverseBlocked,
    SolverError,
    UniverseTooSmall,
    optimize,
)

logger = logging.getLogger(__name__)

WINDOWS_DAYS = (365, 1095, 1825, 3650)
SCENARIOS = ("max_sharpe", "min_volatility")


@shared_task(ignore_result=True)
def run_best_overall_snapshots():
    """One global (account=None) OptimizationSnapshot per (window, scenario)."""
    from marketdata.universe import get_candidate_universe

    universe, _ = get_candidate_universe()
    if len(universe) < 3:
        logger.warning("Candidate universe too small (%d); skipping.", len(universe))
        return {"ok": False, "reason": "universe_too_small"}

    written = []
    for window_days in WINDOWS_DAYS:
        for scenario in SCENARIOS:
            try:
                payload = optimize(
                    scenario=scenario,
                    current_weights={},
                    total_value_tomans=Decimal("1"),
                    user=None,
                    history_days=window_days,
                    universe=universe,
                    universe_mode="market",
                )
            except (UniverseTooSmall, SolverError, MixedUnitUniverseBlocked) as exc:
                logger.info(
                    "%s/%dd not solvable yet: %s", scenario, window_days, exc
                )
                continue
            snap = OptimizationSnapshot.objects.create(
                account=None,
                scenario=scenario,
                window_days=window_days,
                payload=payload,
                price_version=payload.get("price_version", ""),
            )
            written.append(snap.id)
    logger.info("Wrote %d snapshots.", len(written))
    return {"ok": True, "snapshot_ids": written}

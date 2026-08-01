import datetime as dt
from decimal import Decimal

import jdatetime
import numpy as np
import pandas as pd
from django.conf import settings
from django.utils import timezone

from portfolio.models import BacktestRun, BacktestYear
from portfolio.services.deflator import normalize_basis
from portfolio.services.diagnostics import _load_index_returns
from portfolio.services.optimization import SCENARIOS, optimize
from portfolio.services.returns import daily_returns_matrix, resolve_universe

TSE_BUY_COST = float(getattr(settings, "TSE_BUY_COST", 0.00372))
TSE_SELL_COST = float(getattr(settings, "TSE_SELL_COST", 0.0088))
GOLD_FX_SPREAD = float(getattr(settings, "GOLD_FX_SPREAD", 0.01))
RISK_FREE_RATE_ANNUAL = float(getattr(settings, "RISK_FREE_RATE_ANNUAL", 0.30))
TRADING_DAYS_PER_YEAR = 252
TRAINING_YEARS = 3
MIN_TRAINING_OBSERVATIONS = 252
INTEGRITY_VERSION = "windowed-v1:max-forward-fill-5"


def _completed_jalali_cutoffs(now=None, count: int = 5) -> list[str]:
    """Return the starts of the most recently completed Jalali years."""
    now = now or timezone.now()
    current = jdatetime.date.fromgregorian(date=now.date())
    last_completed = current.year - 1
    return [
        f"{year:04d}-01-01"
        for year in range(last_completed - count + 1, last_completed + 1)
    ]


def _jalali_start(year: int) -> dt.datetime:
    gregorian = jdatetime.date(year, 1, 1).togregorian()
    return dt.datetime.combine(gregorian, dt.time.min, tzinfo=dt.timezone.utc)


def _transaction_cost(
    previous: dict[str, float],
    target: dict[str, float],
    source_map: dict[str, str],
) -> tuple[float, float]:
    cost = turnover = 0.0
    for key in set(previous) | set(target):
        delta = float(target.get(key, 0.0)) - float(previous.get(key, 0.0))
        turnover += abs(delta)
        if not delta:
            continue
        if source_map.get(key, "brs") == "tse":
            fee = TSE_BUY_COST if delta > 0 else TSE_SELL_COST
        else:
            fee = GOLD_FX_SPREAD
        cost += abs(delta) * fee
    return cost, turnover


def _simulate_buy_and_hold(
    returns: pd.DataFrame,
    weights: dict[str, float],
    *,
    cost_drag: float,
) -> dict:
    """Evaluate a frozen allocation with transaction costs deducted at entry."""
    columns = list(weights)
    if returns.empty or not columns or any(key not in returns for key in columns):
        raise ValueError("evaluation_data_unavailable")
    observations = returns[columns]
    if observations.isna().any().any():
        raise ValueError("evaluation_missing_prices")
    weight_array = np.array([weights[key] for key in columns], dtype=float)
    if weight_array.sum() <= 0 or not 0 <= cost_drag < 1:
        raise ValueError("evaluation_invalid_weights_or_cost")
    weight_array /= weight_array.sum()

    gross_values = weight_array.copy()
    net_values = weight_array * (1.0 - cost_drag)
    gross_wealth, net_wealth = [], []
    for day_returns in observations.to_numpy():
        gross_values *= 1.0 + day_returns
        net_values *= 1.0 + day_returns
        gross_wealth.append(float(gross_values.sum()))
        net_wealth.append(float(net_values.sum()))

    gross_daily = pd.Series(gross_wealth, index=observations.index).pct_change(
        fill_method=None
    )
    net_daily = pd.Series(net_wealth, index=observations.index).pct_change(
        fill_method=None
    )
    gross_daily.iloc[0] = gross_wealth[0] - 1.0
    net_daily.iloc[0] = net_wealth[0] - 1.0
    return {
        "gross_return": gross_wealth[-1] - 1.0,
        "net_return": net_wealth[-1] - 1.0,
        "cost_drag": gross_wealth[-1] - net_wealth[-1],
        "daily_returns": net_daily,
        "net_wealth": net_wealth,
        "final_values": net_values,
        "columns": columns,
    }


def _realized_metrics(simulation: dict, turnover: float) -> dict:
    daily = simulation["daily_returns"]
    volatility = float(daily.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)) if len(daily) > 1 else 0.0
    excess = daily - RISK_FREE_RATE_ANNUAL / TRADING_DAYS_PER_YEAR
    sharpe = float(excess.mean() / daily.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)) if volatility > 0 else 0.0
    downside = excess.clip(upper=0.0)
    downside_deviation = float(np.sqrt(np.mean(downside ** 2)))
    sortino = float(excess.mean() / downside_deviation * np.sqrt(TRADING_DAYS_PER_YEAR)) if downside_deviation > 0 else 0.0
    wealth = pd.Series([1.0, *simulation["net_wealth"]])
    drawdown = wealth / wealth.cummax() - 1.0
    max_drawdown = float(drawdown.min())
    annualized_return = float(daily.mean() * TRADING_DAYS_PER_YEAR)
    calmar = annualized_return / abs(max_drawdown) if max_drawdown else 0.0
    return {
        "realized_return": float(simulation["net_return"]),
        "net_return": float(simulation["net_return"]),
        "gross_return": float(simulation["gross_return"]),
        "realized_volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": float(calmar),
        "turnover": float(turnover),
        "cost_drag": float(simulation["cost_drag"]),
    }


def _manifest(
    *,
    cutoff: str,
    next_cutoff: str,
    basis: str,
    scenario: str,
    universe: list[str],
    source_map: dict[str, str],
    payload: dict,
    evaluation: pd.DataFrame,
    evaluation_excluded: list[dict],
) -> dict:
    year = int(cutoff[:4])
    exclusions = [*payload.get("excluded_assets", []), *evaluation_excluded]
    return {
        "cutoff": cutoff,
        "training_window": {
            "start": f"{year - TRAINING_YEARS:04d}-01-01",
            "end": cutoff,
            "years": TRAINING_YEARS,
            "observations": payload.get("observations"),
        },
        "evaluation_window": {"start": cutoff, "end": next_cutoff},
        "basis": basis,
        "scenario": scenario,
        "universe": universe,
        "universe_provenance": "explicit" if universe else "current_active_price_presence_proxy",
        "sources": source_map,
        "exclusions": exclusions,
        "constraints": payload.get("constraints_applied", {}),
        "expected_return_method": payload.get(
            "expected_return_method", "historical_arithmetic_mean_annualized_252"
        ),
        "risk_free_rate_annual": RISK_FREE_RATE_ANNUAL,
        "cost_assumptions": {
            "tse_buy": TSE_BUY_COST,
            "tse_sell": TSE_SELL_COST,
            "gold_fx_spread": GOLD_FX_SPREAD,
        },
        "integrity_version": INTEGRITY_VERSION,
        "integrity_results": exclusions,
        "price_version": payload.get("price_version", ""),
        "maximum_source_data_timestamp": (
            evaluation.index.max().isoformat() if not evaluation.empty else None
        ),
        "limitations": [
            "Historical benchmark is unavailable unless governed benchmark history is enabled.",
            "Market discovery is a price-presence proxy without listing and corporate-action provenance.",
        ],
    }


def run_backtest(run_id: int):
    """Execute five completed Jalali years as a three-year walk-forward study."""
    try:
        run = BacktestRun.objects.get(pk=run_id)
    except BacktestRun.DoesNotExist:
        return

    run.status = BacktestRun.Status.RUNNING
    run.progress = 5
    run.error = ""
    run.integrity_version = INTEGRITY_VERSION
    run.save(update_fields=["status", "progress", "error", "integrity_version"])

    try:
        basis = normalize_basis(run.basis)
        run.basis = basis
        run.save(update_fields=["basis"])
        cutoffs = _completed_jalali_cutoffs()
        universe = run.universe or None
        resolved = resolve_universe(universe)
        frozen_universe = [item["key"] for item in resolved]
        source_map = {item["key"]: item["source"] for item in resolved}
        previous_weights = {scenario: {} for scenario in SCENARIOS}

        for step, cutoff in enumerate(cutoffs):
            year = int(cutoff[:4])
            next_cutoff = f"{year + 1:04d}-01-01"
            cutoff_at = _jalali_start(year)
            next_cutoff_at = _jalali_start(year + 1)
            training_days = (cutoff_at - _jalali_start(year - TRAINING_YEARS)).days
            evaluation, evaluation_excluded = daily_returns_matrix(
                history_days=370,
                as_of=next_cutoff_at,
                universe=universe,
                basis=basis,
            )
            evaluation = evaluation[
                (evaluation.index >= cutoff_at) & (evaluation.index < next_cutoff_at)
            ]

            for scenario in SCENARIOS:
                try:
                    payload = optimize(
                        scenario=scenario,
                        current_weights={},
                        total_value_tomans=Decimal("1000000000"),
                        as_of=cutoff_at,
                        universe=universe,
                        basis=basis,
                        history_days=training_days,
                        min_observations=MIN_TRAINING_OBSERVATIONS,
                    )
                    target = payload["target_weights"]
                except Exception as exc:
                    BacktestYear.objects.create(
                        run=run,
                        cutoff_date=cutoff,
                        scenario=scenario,
                        target_weights={},
                        realized_metrics={"error": str(exc)},
                        excluded_symbols=[],
                    )
                    continue

                manifest = _manifest(
                    cutoff=cutoff,
                    next_cutoff=next_cutoff,
                    basis=basis,
                    scenario=scenario,
                    universe=frozen_universe,
                    source_map=source_map,
                    payload=payload,
                    evaluation=evaluation,
                    evaluation_excluded=evaluation_excluded,
                )
                cost_rate, turnover = _transaction_cost(
                    previous_weights[scenario], target, source_map
                )
                try:
                    simulation = _simulate_buy_and_hold(
                        evaluation, target, cost_drag=cost_rate
                    )
                except ValueError as exc:
                    BacktestYear.objects.create(
                        run=run,
                        cutoff_date=cutoff,
                        scenario=scenario,
                        target_weights=target,
                        realized_metrics={"error": str(exc), "manifest": manifest},
                        excluded_symbols=manifest["exclusions"],
                    )
                    continue

                metrics = _realized_metrics(simulation, turnover)
                metrics["transaction_cost_rate"] = cost_rate
                metrics["manifest"] = manifest
                benchmark_delta = None
                benchmark = _load_index_returns(evaluation.index, as_of=next_cutoff_at)
                if benchmark is not None and benchmark.notna().all() and not benchmark.empty:
                    benchmark_return = float((1.0 + benchmark).prod() - 1.0)
                    benchmark_delta = Decimal(
                        str(metrics["realized_return"] - benchmark_return)
                    )

                BacktestYear.objects.create(
                    run=run,
                    cutoff_date=cutoff,
                    scenario=scenario,
                    target_weights=target,
                    realized_metrics=metrics,
                    benchmark_delta=benchmark_delta,
                    excluded_symbols=manifest["exclusions"],
                )

                final_total = float(simulation["final_values"].sum())
                previous_weights[scenario] = {
                    key: float(value / final_total)
                    for key, value in zip(
                        simulation["columns"], simulation["final_values"]
                    )
                } if final_total > 0 else {}

            run.progress = int(5 + (step + 1) / len(cutoffs) * 90)
            run.save(update_fields=["progress"])

        run.status = BacktestRun.Status.READY
        run.progress = 100
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "progress", "completed_at"])
    except Exception as exc:
        run.status = BacktestRun.Status.FAILED
        run.error = str(exc)
        run.save(update_fields=["status", "error"])

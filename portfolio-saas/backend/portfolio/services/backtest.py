import copy
import hashlib
import datetime as dt
from decimal import Decimal
import numpy as np
import pandas as pd
import jdatetime
from django.conf import settings
from django.utils import timezone

from marketdata.models import MarketIndexData, SymbolIntegrity
from portfolio.models import BacktestRun, BacktestYear
from portfolio.services.returns import daily_returns_matrix, normalize_as_of, to_jalali_str, resolve_universe
from portfolio.services.optimization import optimize
from portfolio.services.diagnostics import _load_index_returns

# Rebalancing and transaction cost parameters
TSE_BUY_COST = float(getattr(settings, "TSE_BUY_COST", 0.00372))
TSE_SELL_COST = float(getattr(settings, "TSE_SELL_COST", 0.0088))
GOLD_FX_SPREAD = float(getattr(settings, "GOLD_FX_SPREAD", 0.01))
RISK_FREE_RATE_ANNUAL = float(getattr(settings, "RISK_FREE_RATE_ANNUAL", 0.30))
TRADING_DAYS_PER_YEAR = 252

def run_backtest(run_id: int):
    """Execute the 5-year walk-forward backtest run and update models."""
    try:
        run = BacktestRun.objects.get(pk=run_id)
    except BacktestRun.DoesNotExist:
        return

    run.status = BacktestRun.Status.RUNNING
    run.progress = 5
    run.save()

    try:
        # Define the 5 annual Jalali cutoffs
        cutoffs = ["1400-01-01", "1401-01-01", "1402-01-01", "1403-01-01", "1404-01-01"]
        scenarios = ["max_sharpe", "min_volatility", "risk_parity", "hrp"]

        # Parse universe from JSON list
        universe = run.universe if run.universe else None
        basis = run.basis

        # Track previous year weights to compute transaction costs (rebalance drag)
        # previous_weights structure: {scenario: {asset_key: weight}}
        previous_weights = {s: {} for s in scenarios}

        # Resolve universe details to know sources
        resolved_univ = resolve_universe(universe)
        source_map = {item["key"]: item["source"] for item in resolved_univ}

        total_steps = len(cutoffs)
        for step_idx, cutoff_str in enumerate(cutoffs):
            parts = [int(p) for p in cutoff_str.split("-")]
            cutoff_j = jdatetime.date(parts[0], parts[1], parts[2])
            cutoff_dt = dt.datetime(cutoff_j.togregorian().year, cutoff_j.togregorian().month, cutoff_j.togregorian().day, 23, 59, 59, tzinfo=dt.timezone.utc)

            # Determine end of evaluation year
            parts_next = [parts[0] + 1, parts[1], parts[2]]
            cutoff_next_j = jdatetime.date(parts_next[0], parts_next[1], parts_next[2])
            cutoff_next_dt = dt.datetime(cutoff_next_j.togregorian().year, cutoff_next_j.togregorian().month, cutoff_next_j.togregorian().day, 23, 59, 59, tzinfo=dt.timezone.utc)

            # Load target return matrix for the evaluation year to compute realized performance
            # history_days=365 ensures we load at least a full year's history up to cutoff_next_dt
            df_year, _ = daily_returns_matrix(history_days=365, as_of=cutoff_next_dt, universe=universe, basis=basis)
            # Filter df_year to contain only the evaluation period days
            if not df_year.empty:
                eval_df = df_year[(df_year.index >= cutoff_dt) & (df_year.index < cutoff_next_dt)]
            else:
                eval_df = pd.DataFrame()

            # Load index returns for benchmark tracking
            index_df = None
            if not eval_df.empty:
                index_df = _load_index_returns(eval_df.index, as_of=cutoff_next_dt)

            for scenario in scenarios:
                try:
                    # Step 1: Run optimize at the cutoff to find target weights
                    payload = optimize(
                        scenario=scenario,
                        current_weights={},
                        total_value_tomans=Decimal("1000000000"),
                        as_of=cutoff_dt,
                        universe=universe,
                        basis=basis,
                        history_days=180
                    )
                    target_weights = payload["target_weights"]
                    excluded = payload["excluded_assets"]
                except Exception as e:
                    # In case of optimization failure (e.g. universe too small at Farvardin 1400)
                    # We store empty weights and zero metrics
                    BacktestYear.objects.create(
                        run=run,
                        cutoff_date=cutoff_str,
                        scenario=scenario,
                        target_weights={},
                        realized_metrics={"error": str(e)},
                        excluded_symbols=[]
                    )
                    continue

                # Step 2: Hold target weights forward and evaluate on eval_df
                # If there is no returns data for this year, metrics are empty
                if eval_df.empty or not target_weights:
                    BacktestYear.objects.create(
                        run=run,
                        cutoff_date=cutoff_str,
                        scenario=scenario,
                        target_weights=target_weights,
                        realized_metrics={
                            "realized_return": 0.0,
                            "realized_volatility": 0.0,
                            "sharpe": 0.0,
                            "max_drawdown": 0.0,
                            "cost_drag": 0.0
                        },
                        excluded_symbols=excluded
                    )
                    # Store current weights as target for next step drift
                    previous_weights[scenario] = target_weights
                    continue

                # Align target weights columns with eval_df columns
                cols = [k for k in target_weights if k in eval_df.columns]
                if not cols:
                    BacktestYear.objects.create(
                        run=run,
                        cutoff_date=cutoff_str,
                        scenario=scenario,
                        target_weights=target_weights,
                        realized_metrics={
                            "realized_return": 0.0,
                            "realized_volatility": 0.0,
                            "sharpe": 0.0,
                            "max_drawdown": 0.0,
                            "cost_drag": 0.0
                        },
                        excluded_symbols=excluded
                    )
                    previous_weights[scenario] = target_weights
                    continue

                w_arr = np.array([target_weights[c] for c in cols])
                w_arr = w_arr / w_arr.sum() if w_arr.sum() > 0 else w_arr

                # Daily buy-and-hold portfolio path simulation
                v = w_arr.copy()
                wealths = []
                eval_returns_arr = eval_df[cols].fillna(0.0).to_numpy()
                for day_returns in eval_returns_arr:
                    v = v * (1.0 + day_returns)
                    wealths.append(float(np.sum(v)))

                # Portfolio returns series
                port_daily = pd.Series(wealths, index=eval_df.index).pct_change().fillna(0.0)
                if wealths:
                    port_daily.iloc[0] = wealths[0] - 1.0

                # Metrics calculation
                realized_return = wealths[-1] - 1.0 if wealths else 0.0
                realized_vol = float(np.std(port_daily, ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)) if len(port_daily) > 1 else 0.0
                sharpe = 0.0
                if realized_vol > 0:
                    sharpe = (realized_return - RISK_FREE_RATE_ANNUAL) / realized_vol

                # Max drawdown calculation
                wealth_series = pd.Series([1.0] + wealths)
                running_max = wealth_series.cummax()
                drawdowns = (wealth_series - running_max) / running_max
                max_dd = float(drawdowns.min())

                # Transaction costs (rebalance drag)
                cost_drag = 0.0
                prev_w = previous_weights[scenario]
                # Combine all keys
                all_keys = set(target_weights.keys()) | set(prev_w.keys())
                for key in all_keys:
                    w_new = target_weights.get(key, 0.0)
                    w_old = prev_w.get(key, 0.0)
                    delta = w_new - w_old
                    source = source_map.get(key, "tse")

                    if delta > 0:  # Buy
                        fee = TSE_BUY_COST if source == "tse" else GOLD_FX_SPREAD
                        cost_drag += delta * fee
                    elif delta < 0:  # Sell
                        fee = TSE_SELL_COST if source == "tse" else GOLD_FX_SPREAD
                        cost_drag += abs(delta) * fee

                # Benchmark delta
                benchmark_delta = None
                if index_df is not None and not index_df.empty:
                    # Calculate benchmark return over evaluation year
                    # index_df is already simple returns aligned with eval_df index
                    b_wealth = (1.0 + index_df).cumprod()
                    realized_benchmark_return = float(b_wealth.iloc[-1] - 1.0) if not b_wealth.empty else 0.0
                    benchmark_delta = Decimal(str(realized_return - realized_benchmark_return))

                # Create BacktestYear record
                BacktestYear.objects.create(
                    run=run,
                    cutoff_date=cutoff_str,
                    scenario=scenario,
                    target_weights=target_weights,
                    realized_metrics={
                        "realized_return": float(realized_return),
                        "realized_volatility": float(realized_vol),
                        "sharpe": float(sharpe),
                        "max_drawdown": float(max_dd),
                        "cost_drag": float(cost_drag)
                    },
                    benchmark_delta=benchmark_delta,
                    excluded_symbols=excluded
                )

                # Store drifted weights at the end of year as the old weights for next rebalance
                drifted_weights = {}
                final_wealth = wealths[-1] if wealths else 1.0
                if final_wealth > 0:
                    for col, val in zip(cols, v):
                        drifted_weights[col] = float(val / final_wealth)
                previous_weights[scenario] = drifted_weights

            # Update progress
            run.progress = int(5 + (step_idx + 1) / total_steps * 90)
            run.save()

        # Update run status to ready
        run.status = BacktestRun.Status.READY
        run.progress = 100
        run.completed_at = timezone.now()
        run.save()

    except Exception as e:
        run.status = BacktestRun.Status.FAILED
        run.error = str(e)
        run.save()

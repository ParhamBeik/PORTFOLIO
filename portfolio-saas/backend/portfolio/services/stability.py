from collections import Counter, defaultdict
from statistics import mean, pstdev

from portfolio.models import Asset


def backtest_stability(run) -> dict:
    rows = list(run.years.all())
    cutoffs = sorted({row.cutoff_date for row in rows})
    denominator = len(cutoffs) or 1
    by_cutoff = defaultdict(list)
    for row in rows:
        by_cutoff[row.cutoff_date].append(row)

    selected = defaultdict(set)
    weights = defaultdict(lambda: defaultdict(float))
    exclusion_reasons = Counter()
    ranks = defaultdict(list)
    for cutoff, year_rows in by_cutoff.items():
        for row in year_rows:
            for symbol, weight in row.target_weights.items():
                if float(weight) > 0:
                    selected[symbol].add(cutoff)
                    weights[symbol][cutoff] = max(
                        weights[symbol][cutoff], float(weight)
                    )
            for exclusion in row.excluded_symbols or []:
                reason = exclusion.get("reason") if isinstance(exclusion, dict) else str(exclusion)
                if reason:
                    exclusion_reasons[reason] += 1
        ranked = sorted(
            year_rows,
            key=lambda item: float(item.realized_metrics.get("net_return", float("-inf"))),
            reverse=True,
        )
        for rank, row in enumerate(ranked, start=1):
            ranks[row.scenario].append(rank)

    classes = dict(
        Asset.objects.filter(key__in=selected).values_list("key", "asset_class")
    )
    class_cutoffs = defaultdict(set)
    for symbol, symbol_cutoffs in selected.items():
        class_cutoffs[classes.get(symbol, "Unknown")].update(symbol_cutoffs)

    return {
        "asset_selection_frequency": {
            symbol: len(symbol_cutoffs) / denominator
            for symbol, symbol_cutoffs in sorted(selected.items())
        },
        "average_weight": {
            symbol: sum(by_year.values()) / denominator
            for symbol, by_year in sorted(weights.items())
        },
        "asset_class_persistence": {
            asset_class: len(class_years) / denominator
            for asset_class, class_years in sorted(class_cutoffs.items())
        },
        "scenario_rank_stability": {
            scenario: {
                "average_rank": mean(values),
                "rank_stddev": pstdev(values),
            }
            for scenario, values in sorted(ranks.items())
        },
        "recurring_exclusion_reasons": dict(sorted(exclusion_reasons.items())),
    }

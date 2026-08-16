"""One-shot audit of the μ / σ / ρ panel that feeds My Optimal.

Usage:
    python manage.py audit_optimizer_inputs
    python manage.py audit_optimizer_inputs --days 365 1095
"""
from django.core.management.base import BaseCommand

from portfolio.services.classification import HARD_ASSET_SLEEVE, asset_class_map
from portfolio.services.optimization import (
    DEFAULT_CONSTRAINTS,
    summarize_optimizer_inputs,
)
from portfolio.services.returns import daily_returns_matrix


class Command(BaseCommand):
    help = (
        "Dump annualized mean, vol, Sharpe, and Gold×Cash correlations from "
        "daily_returns_matrix — the same panel the optimizer uses."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            nargs="+",
            default=[365, 1095],
            help="Lookback windows in calendar days (default: 1Y and 3Y).",
        )
        parser.add_argument(
            "--universe",
            nargs="*",
            default=None,
            help="Optional asset keys. Default is the full catalog universe.",
        )

    def handle(self, *args, **options):
        universe = options["universe"] or None
        threshold = float(DEFAULT_CONSTRAINTS["correlation_cluster_threshold"])
        class_map = asset_class_map(universe)
        self.stdout.write(
            f"Hard-asset sleeve: {HARD_ASSET_SLEEVE['label']} "
            f"cap {HARD_ASSET_SLEEVE['max_weight']:.0%} "
            f"(classes {', '.join(HARD_ASSET_SLEEVE['classes'])})"
        )
        for days in options["days"]:
            returns, excluded = daily_returns_matrix(
                history_days=days, universe=universe
            )
            summary = summarize_optimizer_inputs(
                returns, class_map, cluster_threshold=threshold
            )
            self.stdout.write(self.style.SUCCESS(f"\n=== {days}d lookback ==="))
            self.stdout.write(f"observations: {summary['observations']}")
            if excluded:
                dropped = ", ".join(e.get("key", "?") for e in excluded)
                self.stdout.write(f"excluded: {dropped}")
            self.stdout.write(
                f"{'key':<22} {'class':<12} {'mu':>8} {'vol':>8} {'sharpe':>8} flags"
            )
            for row in summary["assets"]:
                flags = ",".join(row["credibility_flags"]) or "-"
                self.stdout.write(
                    f"{row['key']:<22} {row['asset_class']:<12} "
                    f"{row['expected_return_annual']:8.2%} "
                    f"{row['annualized_volatility']:8.2%} "
                    f"{row['sharpe']:8.2f} {flags}"
                )
            self.stdout.write("\nGold × Cash pairs:")
            if not summary["gold_cash_pairs"]:
                self.stdout.write("  (none in this window)")
                continue
            for pair in summary["gold_cash_pairs"]:
                cluster = "cluster" if pair["would_cluster"] else "no-cluster"
                self.stdout.write(
                    f"  {pair['a']} × {pair['b']}: r={pair['correlation']:.2f} ({cluster})"
                )

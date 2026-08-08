"""Read the workflow ledger.

The ledger was built to answer "what did the fetchers actually do", but nothing
read it: no command, no endpoint, only the admin changelist one row at a time.
This is that reader. Strictly read-only.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.utils import timezone

from marketdata.models import WorkflowRun

# Ledger outcomes worth calling out separately; everything else is progress.
_BAD = ("failed", "blocked_network", "blocked_storage")


class Command(BaseCommand):
    help = "Summarize WorkflowRun outcomes over a trailing window."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=float, default=1.0)
        parser.add_argument("--workflow", default="")
        parser.add_argument("--symbol", default="")
        parser.add_argument("--errors", type=int, default=10,
                            help="How many recent failing rows to print.")

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(hours=options["hours"])
        runs = WorkflowRun.objects.filter(created_at__gte=since)
        if options["workflow"]:
            runs = runs.filter(workflow=options["workflow"])
        if options["symbol"]:
            runs = runs.filter(symbol=options["symbol"])

        total = runs.count()
        self.stdout.write(
            f"window={options['hours']}h since={since.isoformat(timespec='seconds')} runs={total}"
        )
        if not total:
            return

        self.stdout.write("\n=== workflow x outcome ===")
        self.stdout.write(f"{'workflow':16} {'outcome':16} {'runs':>7} {'recv':>10} "
                          f"{'acpt':>10} {'rej':>8} {'http':>7} {'ms/run':>8}")
        rows = (
            runs.values("workflow", "outcome")
            .annotate(
                n=Count("id"),
                recv=Sum("rows_received"),
                acpt=Sum("rows_accepted"),
                rej=Sum("rows_rejected"),
                http=Sum("http_attempts"),
                ms=Sum("duration_ms"),
            )
            .order_by("workflow", "-n")
        )
        for row in rows:
            self.stdout.write(
                f"{row['workflow']:16} {row['outcome']:16} {row['n']:7} "
                f"{row['recv'] or 0:10} {row['acpt'] or 0:10} {row['rej'] or 0:8} "
                f"{row['http'] or 0:7} {(row['ms'] or 0) // max(row['n'], 1):8}"
            )

        self.stdout.write("\n=== why (error_code) ===")
        codes = (
            runs.exclude(error_code="")
            .values("error_code", "outcome")
            .annotate(n=Count("id"))
            .order_by("-n")[:20]
        )
        for row in codes:
            self.stdout.write(f"  {row['outcome']:16} {row['error_code'][:48]:48} {row['n']}")
        if not codes:
            self.stdout.write("  (none)")

        self.stdout.write("\n=== endpoint coverage ===")
        for row in (
            runs.exclude(endpoint="")
            .values("endpoint")
            .annotate(n=Count("id"), sym=Count("symbol", distinct=True))
            .order_by("-n")
        ):
            self.stdout.write(f"  {row['endpoint']:30} runs={row['n']:6} symbols={row['sym']}")

        failing = runs.filter(outcome__in=_BAD).order_by("-created_at")[: options["errors"]]
        self.stdout.write(f"\n=== recent {', '.join(_BAD)} ===")
        for run in failing:
            self.stdout.write(
                f"  {run.created_at.isoformat(timespec='seconds')} {run.workflow} "
                f"{run.endpoint} {run.symbol} {run.error_code} {run.correlation_id}"
            )
        if not failing:
            self.stdout.write("  (none)")

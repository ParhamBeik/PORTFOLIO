"""Scrub measurement zeros out of recorded ops snapshots.

Fixing the measurement (see `_hypertables()`) stops new zeros; it does nothing
about the ones already recorded. Those stay on the warehouse-growth chart
forever, and `_fill_rates` reads a *single* snapshot from 24h/7d ago -- if it
draws a phantom zero it reports the whole table as having appeared overnight.

A zero is treated as false only when the same metric is positive both before
and after it. These tables are append-only, so a value cannot legitimately go
41.6M -> 0 -> 41.6M; anything bracketed that way was never measured. The key is
*removed*, not back-filled: the reading is unknown, and a chart with a missing
point is honest where an invented one is not.

    python manage.py repair_metric_zeros              # report only
    python manage.py repair_metric_zeros --apply
"""
from django.core.management.base import BaseCommand

from marketdata.models import OperationalMetricSnapshot

FIELDS = ("database_counts", "table_bytes")


class Command(BaseCommand):
    help = "Remove bracketed zeros from recorded operational metric snapshots."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the repair; otherwise report and exit.")
        parser.add_argument("--days", type=int, default=90,
                            help="How far back to scan (default: 90).")

    def handle(self, *args, **opts):
        from datetime import timedelta

        from django.utils import timezone

        since = timezone.now() - timedelta(days=opts["days"])
        snapshots = list(
            OperationalMetricSnapshot.objects.filter(captured_at__gte=since)
            .order_by("captured_at")
        )
        if not snapshots:
            self.stdout.write("No snapshots in range.")
            return

        dirty, removed = {}, 0
        for field in FIELDS:
            for key in sorted({k for s in snapshots for k in (getattr(s, field) or {})}):
                values = [(getattr(s, field) or {}).get(key) for s in snapshots]
                for i, value in enumerate(values):
                    if value != 0 or not self._bracketed(values, i):
                        continue
                    del getattr(snapshots[i], field)[key]
                    dirty.setdefault(snapshots[i].pk, snapshots[i])
                    removed += 1
                    self.stdout.write(
                        f"{snapshots[i].captured_at:%Y-%m-%d %H:%M}  {field}.{key} = 0"
                    )

        if not removed:
            self.stdout.write(self.style.SUCCESS("No bracketed zeros found."))
            return

        verb = "Removing" if opts["apply"] else "Would remove"
        self.stdout.write(f"\n{verb} {removed} false reading(s) across {len(dirty)} snapshot(s).")
        if opts["apply"]:
            OperationalMetricSnapshot.objects.bulk_update(dirty.values(), FIELDS)
            self.stdout.write(self.style.SUCCESS("Repaired."))
        else:
            self.stdout.write("Re-run with --apply to write.")

    @staticmethod
    def _bracketed(values, i):
        """True when a positive reading exists on both sides of index `i`."""
        return (any(v for v in values[:i] if v)
                and any(v for v in values[i + 1:] if v))

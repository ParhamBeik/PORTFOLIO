"""Re-arm archive states parked by the pacing-vs-exhaustion bug.

Between 2026-08-27 and 2026-09-04 `run_archive_state` deferred every
`QuotaExhausted` to the next quota day, including `archive_paced` -- a refusal
that clears on its own within minutes. Because the backlog all fell due at Tehran
midnight, and midnight is when the paced allowance sits at its floor, the herd
refused itself and re-parked nightly: 6,863 states stuck on
`last_error = "Daily quota unavailable."` while ~4,000 TSETMC requests a day went
unspent.

The scheduler fix stops it recurring, but the already-parked rows still carry a
`next_attempt_at` up to a week out. They would drain on their own over several
days; this pulls them forward now.

Wakeups are SPREAD, never stacked on one timestamp -- re-arming 7,000 states to
`now` would rebuild the exact herd the fix exists to prevent, and they would all
be refused inside one pacing window. Default is a two-hour ramp.

    python manage.py requeue_archive_states --dry-run
    python manage.py requeue_archive_states --spread-minutes 120
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from marketdata.models import ArchiveFetchState

#: What the buggy handler wrote. The current handler appends the reason in
#: parentheses, so this prefix match covers both the old rows and any new ones.
PARKED_ERROR_PREFIX = "Daily quota unavailable"


class Command(BaseCommand):
    help = "Pull forward archive states parked on a quota deferral, spread over a ramp."

    def add_arguments(self, parser):
        parser.add_argument(
            "--spread-minutes", type=int, default=120,
            help="Spread the re-armed wakeups across this many minutes (default 120).",
        )
        parser.add_argument(
            "--endpoint", default="",
            help="Limit to one endpoint (default: all).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change and write nothing.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        spread = max(0, int(options["spread_minutes"])) * 60

        parked = ArchiveFetchState.objects.filter(
            last_error__startswith=PARKED_ERROR_PREFIX,
            next_attempt_at__gt=now,
            suspended_at__isnull=True,
        )
        if options["endpoint"]:
            parked = parked.filter(endpoint=options["endpoint"])

        by_endpoint = dict(
            parked.values_list("endpoint")
            .annotate(n=Count("id"))
            .values_list("endpoint", "n")
        )
        total = sum(by_endpoint.values())

        self.stdout.write(f"Parked states eligible for re-arm: {total}")
        for endpoint, count in sorted(by_endpoint.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {endpoint:<28} {count:>6}")
        if not total:
            return
        latest = parked.order_by("-next_attempt_at").values_list(
            "next_attempt_at", flat=True
        ).first()
        self.stdout.write(f"Furthest current wakeup: {latest}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing written."))
            return

        # Chunked so one enormous bulk_update does not hold a long transaction on
        # a table the scheduler is actively leasing from.
        updated = 0
        while True:
            chunk = list(parked.order_by("pk")[:1000])
            if not chunk:
                break
            for state in chunk:
                state.next_attempt_at = now + timedelta(seconds=random.uniform(0, spread))
                # Clearing the error matters: `last_error` is what the Ops console
                # and the coverage classifier read to decide a state is unhealthy,
                # and a quota deferral is not a fault of the state.
                state.last_error = ""
            ArchiveFetchState.objects.bulk_update(
                chunk, ["next_attempt_at", "last_error"], batch_size=500
            )
            updated += len(chunk)
            self.stdout.write(f"  re-armed {updated}/{total}")

        self.stdout.write(self.style.SUCCESS(
            f"Re-armed {updated} states across the next {spread // 60} minutes."
        ))

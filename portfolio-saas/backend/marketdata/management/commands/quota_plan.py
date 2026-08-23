"""Show how today's provider quota is budgeted and what the scheduler would claim.

Read-only. Exists so the spending plan can be checked against production data
*before* a change ships, rather than inferred afterwards from a day that already
ran out. Answers three questions in one place: what live costs, what that leaves
the archive, and which states the current ordering would actually fetch next.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import F, Q
from django.utils import timezone

from marketdata import live_states, quota
from marketdata.archive import _starvation_rank_qs
from marketdata.models import ApiRequestQuota, ArchiveFetchState, LiveFetchState


class Command(BaseCommand):
    help = "Report the day's live plan, derived archive budget, and next claims."

    def add_arguments(self, parser):
        parser.add_argument(
            "--preview", type=int, default=20,
            help="How many upcoming claims to list (0 to skip).",
        )

    def handle(self, *args, **options):
        day = quota.quota_day()
        row = ApiRequestQuota.objects.filter(day=day).first() or ApiRequestQuota(
            day=day, limit=settings.MARKETDATA_DAILY_REQUEST_LIMIT
        )

        self.stdout.write(self.style.MIGRATE_HEADING(f"Quota day {day}"))
        self._live_plan()
        self._budgets(row)
        preview = options["preview"]
        if preview:
            self._preview(preview)

    def _live_plan(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\nLive plan (full 24h)"))
        total = 0
        for endpoint_key in (
            LiveFetchState.objects.filter(enabled=True)
            .values_list("endpoint_key", flat=True)
            .distinct()
            .order_by("endpoint_key")
        ):
            states = list(
                LiveFetchState.objects.filter(enabled=True, endpoint_key=endpoint_key)
            )
            cost = live_states.full_day_cost(states)
            total += cost
            cadence = states[0].cadence_seconds if states else 0
            gate = "session" if states and states[0].session_only else "24h"
            self.stdout.write(
                f"  {endpoint_key:<20} rows={len(states):<5} "
                f"cadence={cadence:<7}s {gate:<8} ~{cost}/day"
            )
        if not total:
            self.stdout.write(
                self.style.WARNING("  no LiveFetchState rows -- run a snapshot task to seed")
            )
        self.stdout.write(f"  {'price loop':<20} {'':<24} ~{self._price_loop()}/day")
        plan = total + self._price_loop()
        self.stdout.write(f"  {'TOTAL':<20} {'':<24} ~{plan}/day")

        ceiling = quota.bucket_budget(quota.LIVE)
        if plan > ceiling:
            self.stdout.write(self.style.ERROR(
                f"  PLAN EXCEEDS THE LIVE CEILING ({plan} > {ceiling}). Live fetches will "
                f"be refused once the bucket is spent -- raise "
                f"MARKETDATA_LIVE_REQUEST_FLOOR or slow a cadence."
            ))

    @staticmethod
    def _price_loop():
        start = live_states.day_start()
        return quota._simulate_price_loop(start, start + timedelta(days=1))

    def _budgets(self, row):
        self.stdout.write(self.style.MIGRATE_HEADING("\nBudgets"))
        for bucket in (quota.LIVE, quota.ARCHIVE, quota.OTHER):
            used = getattr(row, f"{bucket}_used", 0)
            self.stdout.write(
                f"  {bucket:<10} budget={quota.bucket_budget(bucket):<7} "
                f"used={used:<7} remaining={quota.remaining_requests(bucket)}"
            )
        self.stdout.write(
            f"  reserved for live between now and rollover: "
            f"{quota.live_reserve_remaining(row)}"
        )

    def _preview(self, limit):
        """What claim_archive_batch would take next, without leasing anything."""
        now = timezone.now()
        due = Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
        base = ArchiveFetchState.objects.filter(due, suspended_at__isnull=True)
        tick = ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS
        order = (
            "_coverage_rank", "-_deficit", F("last_success_at").asc(nulls_first=True),
        )

        share = settings.MARKETDATA_TICK_QUOTA_SHARE
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nNext {limit} claims (tick share {share:.0%})"
        ))
        virgin = base.filter(last_success_at__isnull=True).count()
        self.stdout.write(f"  never-succeeded states waiting: {virgin}")

        lanes = (
            ("tick", _starvation_rank_qs(
                base.filter(endpoint=tick, verified_complete=False)
            ).order_by(*order)[: int(limit * share)]),
            ("general", _starvation_rank_qs(
                base.exclude(endpoint=tick)
            ).order_by(*order)[: limit - int(limit * share)]),
        )
        for lane, states in lanes:
            for state in states:
                self.stdout.write(
                    f"  [{lane:<7}] {state.endpoint:<26} {state.symbol:<14} "
                    f"deficit={state._deficit:<6} last_success={state.last_success_at}"
                )

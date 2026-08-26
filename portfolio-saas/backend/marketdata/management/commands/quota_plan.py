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
        rows = {
            row.plan: row for row in ApiRequestQuota.objects.filter(day=day)
        }

        self.stdout.write(self.style.MIGRATE_HEADING(f"Quota day {day}"))
        self._live_plan()
        for plan in quota.PLANS:
            self._budgets(plan, rows.get(plan) or ApiRequestQuota(day=day, plan=plan))
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
        self.stdout.write("  (TOTAL is not a shared wallet -- compare each plan below.)")

        for p in quota.PLANS:
            loop = self._price_loop(p)
            cap = quota.bucket_budget(quota.LIVE, p)
            self.stdout.write(f"  price loop {p:<8} ~{loop}/day  live cap={cap}")
            if loop > cap:
                self.stdout.write(self.style.ERROR(
                    f"  {p} PRICE LOOP EXCEEDS ITS LIVE CAP ({loop} > {cap}). "
                    f"Live fetches on that wallet will be refused once the bucket "
                    f"is spent -- raise MARKETDATA_LIVE_REQUEST_FLOOR or slow a cadence."
                ))

    @staticmethod
    def _price_loop(plan=None):
        start = live_states.day_start()
        return quota._simulate_price_loop(start, start + timedelta(days=1), plan=plan)

    def _budgets(self, plan, row):
        live_blocked = quota.is_plan_blocked(plan, bucket=quota.LIVE)
        archive_blocked = quota.is_plan_blocked(plan, bucket=quota.ARCHIVE)
        disclosed = row.limit or 0
        assumed = quota.effective_limit(plan, row)
        ceiling = disclosed if disclosed else f"assumed {assumed}"
        if live_blocked:
            tag = " [LIVE BLOCKED until reset]"
        elif archive_blocked:
            tag = " [ARCHIVE BLOCKED until reset]"
        else:
            tag = ""
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nPlan {plan} -- used {row.used}/{ceiling}{tag}"
        ))
        for bucket in (quota.LIVE, quota.ARCHIVE, quota.OTHER):
            used = getattr(row, f"{bucket}_used", 0)
            budget = quota.bucket_budget(bucket, plan)
            self.stdout.write(
                f"  {bucket:<10} budget={str(budget if budget is not None else 'uncapped'):<9} "
                f"used={used:<7} remaining={quota.remaining_requests(bucket, plan)}"
            )
        self.stdout.write(
            f"  reserved for live between now and rollover: "
            f"{quota.live_reserve_remaining(plan, row)}"
        )
        self.stdout.write(
            f"  archive paced now/day: "
            f"{quota.archive_allowance_now(plan, row)}/"
            f"{quota.archive_day_ceiling(plan, row)}"
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

"""Restamp an existing property mark to its real purchase date and rebuild snapshots."""
from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date

from portfolio.models import Holding, LedgerEntry
from portfolio.services.ledger import HOUSE_MARK_KINDS, backfill_house_into_snapshots


class Command(BaseCommand):
    help = (
        "Move a property's live ledger mark to --occurred-at, then add that "
        "house into snapshot points taken before the holding was created."
    )

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true")
        parser.add_argument("--holding-id", type=int)
        parser.add_argument("--occurred-at", help="ISO date or datetime (purchase date)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if options["list"]:
            for h in Holding.objects.filter(asset__is_house=True).select_related(
                "account", "asset", "account__user"
            ):
                marks = LedgerEntry.objects.filter(
                    account=h.account, asset=h.asset, kind__in=HOUSE_MARK_KINDS,
                    reversal_of__isnull=True, reversed_by__isnull=True,
                ).order_by("timestamp")
                stamp = ", ".join(
                    f"{m.kind}@{m.timestamp.date()} note={m.note!r}" for m in marks
                ) or "no marks"
                self.stdout.write(
                    f"holding={h.id} account={h.account_id} "
                    f"user={h.account.user_id} name={h.asset.name!r} "
                    f"area={h.area_sqm} created={h.created_at.date()} {stamp}"
                )
            return

        holding_id = options["holding_id"]
        raw = options["occurred_at"]
        if not holding_id or not raw:
            raise CommandError("Pass --holding-id and --occurred-at, or --list.")
        when = parse_datetime(raw)
        if when is None:
            day = parse_date(raw)
            if day is None:
                raise CommandError(f"Could not parse occurred-at: {raw}")
            when = datetime(day.year, day.month, day.day, tzinfo=dt_timezone.utc)
        if timezone.is_naive(when):
            when = timezone.make_aware(when, timezone.get_current_timezone())
        if when > timezone.now():
            raise CommandError("Purchase date cannot be in the future.")

        holding = Holding.objects.select_related("account", "asset").get(pk=holding_id)
        if not holding.asset.is_house:
            raise CommandError("Holding is not a property.")
        marks = list(
            LedgerEntry.objects.filter(
                account=holding.account, asset=holding.asset,
                kind__in=HOUSE_MARK_KINDS,
                reversal_of__isnull=True, reversed_by__isnull=True,
            ).order_by("timestamp")
        )
        if not marks:
            raise CommandError("No live house marks to restamp.")
        first = marks[0]
        self.stdout.write(
            f"holding={holding.id} {first.kind} {first.timestamp} -> {when}"
        )
        if options["dry_run"]:
            return
        first.timestamp = when
        first.save(update_fields=["timestamp"])
        n = backfill_house_into_snapshots(
            holding.account, holding.asset, before=holding.created_at,
        )
        self.stdout.write(f"updated mark; backfilled {n} snapshot rows")

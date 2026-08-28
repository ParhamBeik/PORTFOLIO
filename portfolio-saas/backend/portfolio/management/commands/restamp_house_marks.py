"""Restamp an existing property mark to its real purchase date and rebuild snapshots."""
from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date

from portfolio.models import Holding, LedgerEntry
from portfolio.services.ledger import (
    HOUSE_MARK_KINDS,
    backfill_house_into_snapshots,
    remove_house_from_snapshots,
)


class Command(BaseCommand):
    help = (
        "Move a property's live ledger mark to --occurred-at, then add that "
        "house into snapshot points taken before the holding was created."
    )

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true")
        parser.add_argument("--holding-id", type=int)
        parser.add_argument("--occurred-at", help="ISO date or datetime (purchase date)")
        parser.add_argument(
            "--backfill-before",
            help="Only add this house to snapshots older than this instant. "
                 "Defaults to the earlier of the holding's creation and its "
                 "current first mark -- the point from which snapshots already "
                 "contain it. Widening this double-counts.",
        )
        parser.add_argument(
            "--unbackfill",
            action="store_true",
            help="Do not move any mark. Instead SUBTRACT this house's mark value "
                 "from snapshots older than --backfill-before, undoing a backfill "
                 "that should not have run (e.g. against synthetic gap-fill rows "
                 "that already contained the holding).",
        )
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
        if options["unbackfill"]:
            return self._unbackfill(holding_id, options)
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

        # `backfill_house_into_snapshots` ADDS to each row in place and has no
        # marker saying it already ran, so the bound decides between a correct
        # history and a silently doubled one. A snapshot already contains this
        # house from whichever came first:
        #   * the Holding row existing -- the price loop photographs
        #     `account.holdings.all()`, marks not consulted; or
        #   * the earliest live mark -- HoldingListCreateView backfilled from
        #     there when the property was created.
        # Passing `holding.created_at` alone double-counted every day between a
        # backdated mark and the day the property was typed in.
        boundary = options.get("backfill_before")
        if boundary:
            cutoff = parse_datetime(boundary) or parse_date(boundary)
            if cutoff is None:
                raise CommandError(f"Could not parse backfill-before: {boundary}")
            if not isinstance(cutoff, datetime):
                cutoff = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=dt_timezone.utc)
            if timezone.is_naive(cutoff):
                cutoff = timezone.make_aware(cutoff, timezone.get_current_timezone())
        else:
            cutoff = min(holding.created_at, first.timestamp)

        # An opening carries the account's baseline (`tracking_started_at`), and
        # `create_ledger_entry` refuses any later opening stamped differently.
        # Moving one off that instant desynchronises it from every other opening
        # on the account, so the mark becomes a valuation mark -- which for a
        # house `_projection_state` and `house_state_as_of` treat identically.
        baseline = holding.account.tracking_started_at
        rekind = (
            first.kind == LedgerEntry.Kind.OPENING_POSITION
            and baseline is not None
            and when != baseline
        )
        self.stdout.write(
            f"holding={holding.id} {first.kind} {first.timestamp} -> {when}"
            + (f" (kind -> {LedgerEntry.Kind.VALUATION_MARK})" if rekind else "")
            + f"; backfill snapshots before {cutoff}"
        )
        if options["dry_run"]:
            return
        first.timestamp = when
        fields = ["timestamp"]
        if rekind:
            first.kind = LedgerEntry.Kind.VALUATION_MARK
            fields.append("kind")
        first.save(update_fields=fields)
        n = backfill_house_into_snapshots(
            holding.account, holding.asset, before=cutoff,
        )
        self.stdout.write(f"updated mark; backfilled {n} snapshot rows")

    def _unbackfill(self, holding_id, options):
        """Subtract a house's mark value from snapshots older than the bound.

        The inverse of the backfill this command normally performs, for when it
        was applied to rows that already contained the house -- synthetic
        gap-fill snapshots value the then-current holdings, so a property that
        existed when the gap-fill ran is in every one of them regardless of date.
        """
        raw = options.get("backfill_before")
        if not holding_id or not raw:
            raise CommandError("--unbackfill needs --holding-id and --backfill-before.")
        cutoff = parse_datetime(raw) or parse_date(raw)
        if cutoff is None:
            raise CommandError(f"Could not parse backfill-before: {raw}")
        if not isinstance(cutoff, datetime):
            cutoff = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=dt_timezone.utc)
        if timezone.is_naive(cutoff):
            cutoff = timezone.make_aware(cutoff, timezone.get_current_timezone())

        holding = Holding.objects.select_related("account", "asset").get(pk=holding_id)
        if not holding.asset.is_house:
            raise CommandError("Holding is not a property.")
        self.stdout.write(
            f"holding={holding.id} {holding.asset.name!r}: subtracting its mark "
            f"value from snapshots before {cutoff}"
        )
        if options["dry_run"]:
            return
        n = remove_house_from_snapshots(
            holding.account, holding.asset, before=cutoff,
        )
        self.stdout.write(f"corrected {n} snapshot rows")

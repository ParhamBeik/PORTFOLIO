"""Django management command to audit and repair mispriced database rows.

Usage:
    python manage.py clean_mispriced_data [--fix | --dry-run]
"""
import logging
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction

from portfolio.models import Asset, Price, Snapshot

logger = logging.getLogger(__name__)

MAX_ALLOWED_JUMP_RATIO = Decimal("0.15")  # 15% max jump vs. the trailing baseline
BASELINE_WINDOW_SIZE = 5  # number of confirmed-good prices used to compute the baseline


class Command(BaseCommand):
    help = "Scan database for mispriced data rows (price spikes >15%), erase or replace them, and rebuild snapshots."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--fix",
            action="store_true",
            help="Perform database modifications (delete/update corrupt rows). Default is dry-run mode.",
        )
        group.add_argument(
            "--dry-run",
            action="store_true",
            help="Scan database without making permanent changes (default behavior).",
        )

    def handle(self, *args, **options):
        is_fix_mode = options["fix"] and not options["dry_run"]
        mode_str = "FIX MODE" if is_fix_mode else "DRY-RUN MODE"
        self.stdout.write(self.style.SUCCESS(f"=== Starting Database Price Repair Audit ({mode_str}) ==="))

        stats = audit_and_repair_prices(fix=is_fix_mode)

        self.stdout.write(
            self.style.SUCCESS(
                f"\n=== Audit Complete ===\n"
                f"Total Price Rows Inspected: {stats['total_inspected']}\n"
                f"Flagged Price Spikes (>15% jump): {stats['flagged_spikes']}\n"
                f"Repaired/Erased Price Rows: {stats['repaired_prices']}\n"
                f"Purged Corrupt Snapshots: {stats['purged_snapshots']}\n"
            )
        )


def audit_and_repair_prices(fix: bool = False) -> dict:
    """Core logic to detect sporadic price spikes, erase/replace corrupted rows, and rebuild snapshots.

    Spike detection compares each price to the median of the last
    BASELINE_WINDOW_SIZE confirmed-good prices, not the literal previous row —
    so one flagged spike doesn't become the new baseline and cause the
    following (correct) recovery price to be falsely flagged too.
    """
    stats = {
        "total_inspected": 0,
        "flagged_spikes": 0,
        "repaired_prices": 0,
        "purged_snapshots": 0,
        "details": [],
    }

    assets = Asset.objects.filter(is_active=True)
    corrupt_price_ids = []

    for asset in assets:
        prices = list(Price.objects.filter(asset=asset).order_by("fetched_at", "id"))
        stats["total_inspected"] += len(prices)
        if len(prices) < 2:
            continue

        baseline_window = [Decimal(str(prices[0].price))]

        for i in range(1, len(prices)):
            curr_price = Decimal(str(prices[i].price))

            if curr_price <= 0:
                stats["flagged_spikes"] += 1
                corrupt_price_ids.append(prices[i].id)
                stats["details"].append(
                    f"Asset '{asset.key}' @ {prices[i].fetched_at}: Invalid zero price ({curr_price})"
                )
                continue

            sorted_window = sorted(baseline_window)
            baseline = sorted_window[len(sorted_window) // 2]

            if baseline <= 0:
                baseline_window.append(curr_price)
                baseline_window = baseline_window[-BASELINE_WINDOW_SIZE:]
                continue

            ratio = abs(curr_price - baseline) / baseline
            if ratio > MAX_ALLOWED_JUMP_RATIO:
                stats["flagged_spikes"] += 1
                corrupt_price_ids.append(prices[i].id)
                stats["details"].append(
                    f"Asset '{asset.key}' @ {prices[i].fetched_at}: Spike detected! Baseline={baseline}, Curr={curr_price} (+{float(ratio*100):.1f}%)"
                )
                # A flagged spike doesn't get folded into the baseline, so the
                # next (likely correct) price is compared against the last
                # known-good window, not the spike itself.
                continue

            baseline_window.append(curr_price)
            baseline_window = baseline_window[-BASELINE_WINDOW_SIZE:]

    if fix:
        with transaction.atomic():
            if corrupt_price_ids:
                deleted_prices, _ = Price.objects.filter(id__in=corrupt_price_ids).delete()
                stats["repaired_prices"] = deleted_prices

            # Purge snapshots relying on corrupted valuations, scoped PER USER.
            # A single database-wide median let one whale (or test) account's
            # real large snapshots trigger deletion of unrelated users' data.
            # Runs independently of corrupt_price_ids: bad snapshots can exist
            # even when every Price row is clean (e.g. a one-off bad write).
            purged = 0
            user_ids = Snapshot.objects.values_list("user_id", flat=True).distinct()
            for user_id in user_ids:
                user_snaps = Snapshot.objects.filter(user_id=user_id)
                values = [s.total_value_tomans for s in user_snaps if s.total_value_tomans > 0]
                if not values:
                    continue
                median_snap = sorted(values)[len(values) // 2]
                deleted_snaps, _ = user_snaps.filter(
                    total_value_tomans__gt=median_snap * Decimal("5.0")
                ).delete()
                purged += deleted_snaps
            stats["purged_snapshots"] = purged

            logger.info(
                "[DB_CLEANUP_EXEC] Erased %d corrupt prices and %d corrupt snapshots.",
                stats["repaired_prices"], stats["purged_snapshots"],
            )

    return stats

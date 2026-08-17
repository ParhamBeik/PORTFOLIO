"""Snapshot retention: collapse old raw Snapshot rows into one daily average.

Kept out of `portfolio/tasks.py` deliberately (a concurrent edit is in progress
on that file); this module is self-contained and wired into Celery directly
from `config/celery.py` instead of relying on app autodiscovery.
"""
import datetime as dt
import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Avg
from django.db.models.functions import TruncDate
from django.utils import timezone

from portfolio.models import Price, Snapshot

logger = logging.getLogger(__name__)


def _stale_daily_groups(cutoff):
    return (
        Snapshot.objects.filter(timestamp__lt=cutoff)
        .annotate(day=TruncDate("timestamp"))
        .values("user_id", "account_id", "day")
        .annotate(avg_total=Avg("total_value_tomans"))
        .order_by()
    )


@shared_task(ignore_result=True)
def prune_snapshots():
    """Collapse Snapshot rows older than SNAPSHOT_RETENTION_DAYS to one/day.

    # ponytail: single-pass delete over the whole table; batch by user if the
    # first production run locks too long for the table's size at that point.
    Disabled by default (SNAPSHOT_PRUNE_ENABLED=0): logs what it would do and
    returns without touching any row. This is a data-deleting operation and
    must only be enabled with explicit sign-off.
    """
    cutoff = timezone.now() - dt.timedelta(days=settings.SNAPSHOT_RETENTION_DAYS)
    groups = list(_stale_daily_groups(cutoff))
    stale_row_count = Snapshot.objects.filter(timestamp__lt=cutoff).count()

    if not settings.SNAPSHOT_PRUNE_ENABLED:
        logger.info(
            "Would collapse %d stale rows into %d daily "
            "rows (cutoff=%s). Set SNAPSHOT_PRUNE_ENABLED=1 to actually run this.",
            stale_row_count, len(groups), cutoff.isoformat(),
        )
        result = {"enabled": False, "would_collapse": stale_row_count, "would_write": len(groups)}
        _ledger_prune("prune_snapshots", "Snapshot", result)
        return result

    written = 0
    with transaction.atomic():
        for group in groups:
            Snapshot.objects.filter(
                user_id=group["user_id"],
                account_id=group["account_id"],
                timestamp__date=group["day"],
                timestamp__lt=cutoff,
            ).delete()
            day_start = timezone.make_aware(dt.datetime.combine(group["day"], dt.time.min))
            Snapshot.objects.create(
                user_id=group["user_id"],
                account_id=group["account_id"],
                total_value_tomans=group["avg_total"],
                timestamp=day_start,
                is_estimated=True,
            )
            written += 1
    logger.info(
        "Collapsed %d day-groups (%d stale rows) into %d rows.",
        written, stale_row_count, written,
    )
    result = {"enabled": True, "groups_collapsed": written, "stale_rows_seen": stale_row_count}
    _ledger_prune("prune_snapshots", "Snapshot", result)
    return result


@shared_task(ignore_result=True)
def prune_prices():
    """Drop intra-day Price rows older than PRICE_RETENTION_DAYS; keep latest per asset."""
    cutoff = timezone.now() - dt.timedelta(days=settings.PRICE_RETENTION_DAYS)
    stale = Price.objects.filter(fetched_at__lt=cutoff)
    stale_count = stale.count()
    if not settings.PRICE_PRUNE_ENABLED:
        logger.info(
            "Would delete %d price rows older than %s. "
            "Set PRICE_PRUNE_ENABLED=1 to run.",
            stale_count, cutoff.isoformat(),
        )
        result = {"enabled": False, "would_delete": stale_count}
        _ledger_prune("prune_prices", "Price", result)
        return result

    latest_ids = list(
        Price.objects.order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
        .values_list("id", flat=True)
    )
    deleted, _ = stale.exclude(id__in=latest_ids).delete()
    logger.info("Deleted %d stale price rows (kept latest per asset).", deleted)
    result = {"enabled": True, "deleted": deleted, "kept_latest": len(latest_ids)}
    _ledger_prune("prune_prices", "Price", result)
    return result


def _ledger_prune(workflow, table, metadata):
    from marketdata.models import WorkflowRun
    from marketdata.workflows import WorkflowOutcome

    WorkflowOutcome(workflow, destination_table=table).finish(
        WorkflowRun.Outcome.SUCCESS,
        metadata=metadata,
    )

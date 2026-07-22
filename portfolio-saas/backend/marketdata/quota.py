"""Atomic daily provider quota shared by every web and Celery process."""
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import ApiRequestQuota

ARCHIVE = "archive"
LIVE = "live"
OTHER = "other"


class QuotaExhausted(RuntimeError):
    pass


def quota_day():
    zone = ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE)
    return timezone.now().astimezone(zone).date()


def reserve_request(bucket=OTHER):
    limit = settings.MARKETDATA_DAILY_REQUEST_LIMIT
    non_archive_limit = limit - settings.MARKETDATA_ARCHIVE_REQUEST_RESERVE
    with transaction.atomic():
        row, _ = ApiRequestQuota.objects.select_for_update().get_or_create(
            day=quota_day(),
            defaults={"limit": limit},
        )
        if row.limit != limit:
            row.limit = limit
        if row.used >= row.limit:
            raise QuotaExhausted("Daily API request quota exhausted.")
        if bucket != ARCHIVE and row.live_used + row.other_used >= non_archive_limit:
            raise QuotaExhausted("Non-archive API request reserve exhausted.")
        row.used += 1
        field = f"{bucket}_used"
        setattr(row, field, getattr(row, field) + 1)
        row.save(update_fields=["limit", "used", field, "updated_at"])
        return row.limit - row.used


def remaining_requests():
    row = ApiRequestQuota.objects.filter(day=quota_day()).first()
    return settings.MARKETDATA_DAILY_REQUEST_LIMIT - (row.used if row else 0)

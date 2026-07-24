"""Atomic daily and 5-minute window provider quota shared by every web and Celery process."""
import collections
import time
import uuid
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from portfolio.live.pubsub import get_redis
from .models import ApiRequestQuota

ARCHIVE = "archive"
LIVE = "live"
OTHER = "other"

_LOCAL_WINDOW = collections.deque()


class QuotaExhausted(RuntimeError):
    pass


def quota_day():
    zone = ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE)
    return timezone.now().astimezone(zone).date()


def _check_and_record_window():
    """Enforce rolling 5-minute request window quota (default 1,000 req / 300s)."""
    window_limit = getattr(settings, "MARKETDATA_WINDOW_LIMIT", 1000)
    window_seconds = getattr(settings, "MARKETDATA_WINDOW_SECONDS", 300)
    now = time.time()
    client = get_redis()
    if client is not None:
        try:
            key = "quota:window:5m"
            current_count = client.eval(
                "redis.call('zremrangebyscore', KEYS[1], 0, ARGV[1]); "
                "local count = redis.call('zcard', KEYS[1]); "
                "if count >= tonumber(ARGV[4]) then return -1 end; "
                "redis.call('zadd', KEYS[1], ARGV[2], ARGV[3]); "
                "redis.call('expire', KEYS[1], ARGV[5]); "
                "return count + 1",
                1,
                key,
                now - window_seconds,
                now,
                uuid.uuid4().hex,
                window_limit,
                window_seconds + 60,
            )
            if current_count < 0:
                raise QuotaExhausted(
                    f"5-minute window API request limit reached ({window_limit}/{window_limit} req)."
                )
            return current_count
        except QuotaExhausted:
            raise
        except Exception:
            pass

    # Fallback to local memory sliding window
    cutoff = now - window_seconds
    while _LOCAL_WINDOW and _LOCAL_WINDOW[0] < cutoff:
        _LOCAL_WINDOW.popleft()
    if len(_LOCAL_WINDOW) >= window_limit:
        raise QuotaExhausted(
            f"5-minute window API request limit reached ({len(_LOCAL_WINDOW)}/{window_limit} req)."
        )
    _LOCAL_WINDOW.append(now)
    return len(_LOCAL_WINDOW)


def reserve_request(bucket=OTHER):
    limit = settings.MARKETDATA_DAILY_REQUEST_LIMIT
    archive_reserve = settings.MARKETDATA_ARCHIVE_REQUEST_RESERVE
    non_archive_limit = limit - archive_reserve
    with transaction.atomic():
        row, _ = ApiRequestQuota.objects.select_for_update().get_or_create(
            day=quota_day(),
            defaults={"limit": limit},
        )
        if row.limit != limit:
            row.limit = limit
        if row.used >= row.limit:
            raise QuotaExhausted("Daily API request quota exhausted.")

        if bucket == ARCHIVE:
            if archive_reserve > 0 and row.archive_used >= archive_reserve:
                raise QuotaExhausted("Daily archive API reserve exhausted.")
        else:
            if row.live_used + row.other_used >= non_archive_limit:
                raise QuotaExhausted("Non-archive API request reserve exhausted.")

        _check_and_record_window()

        row.used += 1
        field = f"{bucket}_used"
        setattr(row, field, getattr(row, field) + 1)
        row.save(update_fields=["limit", "used", field, "updated_at"])
        return row.limit - row.used


def remaining_requests():
    row = ApiRequestQuota.objects.filter(day=quota_day()).first()
    return settings.MARKETDATA_DAILY_REQUEST_LIMIT - (row.used if row else 0)


def get_quota_status():
    """Detailed quota metrics: daily usage history, remaining quotas, and 5-min window status."""
    day = quota_day()
    limit = settings.MARKETDATA_DAILY_REQUEST_LIMIT
    row = ApiRequestQuota.objects.filter(day=day).first()
    used = row.used if row else 0
    archive_used = row.archive_used if row else 0
    live_used = row.live_used if row else 0
    other_used = row.other_used if row else 0

    window_limit = getattr(settings, "MARKETDATA_WINDOW_LIMIT", 1000)
    window_seconds = getattr(settings, "MARKETDATA_WINDOW_SECONDS", 300)
    window_used = 0
    client = get_redis()
    if client is not None:
        try:
            now = time.time()
            key = "quota:window:5m"
            client.zremrangebyscore(key, 0, now - window_seconds)
            window_used = client.zcard(key)
        except Exception:
            window_used = len(_LOCAL_WINDOW)
    else:
        cutoff = time.time() - window_seconds
        window_used = sum(1 for ts in _LOCAL_WINDOW if ts >= cutoff)

    history = list(
        ApiRequestQuota.objects.all().order_by("-day")[:30].values(
            "day", "limit", "used", "archive_used", "live_used", "other_used", "updated_at"
        )
    )
    for h in history:
        h["day"] = str(h["day"])
        h["updated_at"] = h["updated_at"].isoformat() if h["updated_at"] else ""

    return {
        "day": str(day),
        "limit": limit,
        "used": used,
        "remaining_daily": max(0, limit - used),
        "archive_used": archive_used,
        "live_used": live_used,
        "other_used": other_used,
        "window_limit": window_limit,
        "window_seconds": window_seconds,
        "window_used": window_used,
        "remaining_window": max(0, window_limit - window_used),
        "quota_history": history,
    }

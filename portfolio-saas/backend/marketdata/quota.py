"""Atomic daily and 5-minute window provider quota shared by every web and Celery process."""
import collections
import logging
import math
import time
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from portfolio.live.pubsub import get_redis
from .models import ApiRequestQuota

logger = logging.getLogger(__name__)

ARCHIVE = "archive"
LIVE = "live"
OTHER = "other"

# Shares of the provider's single 1,000-per-5-minute window. They sum to 1.0 so
# the three buckets together can never exceed what the provider allows.
# ponytail: fixed shares; promote to settings only if a bucket measurably starves.
_WINDOW_SHARE = {LIVE: 0.20, ARCHIVE: 0.75, OTHER: 0.05}

_LOCAL_WINDOWS = collections.defaultdict(collections.deque)

# Assumed worker-process count when the shared Redis limiter is down: each process
# gets 1/Nth of the window so N of them together stay under the provider's real
# limit. Over-estimating only throttles us; under-estimating gets us blocked.
_DEGRADED_PROCESS_DIVISOR = 10


def get_historical_full_used_today():
    client = get_redis()
    if client is not None:
        try:
            key = f"quota:historical_full:used:{quota_day()}"
            val = client.get(key)
            return int(val) if val else 0
        except Exception:
            return 0
    return 0


def increment_historical_full_used():
    client = get_redis()
    if client is not None:
        try:
            key = f"quota:historical_full:used:{quota_day()}"
            client.incr(key)
            client.expire(key, 172800)  # 2 days TTL
        except Exception:
            pass


class QuotaExhausted(RuntimeError):
    pass


def quota_day():
    zone = ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE)
    return timezone.now().astimezone(zone).date()


def bucket_budget(bucket):
    if bucket == LIVE:
        return (
            settings.MARKETDATA_LIVE_REQUEST_FLOOR
            + settings.MARKETDATA_LIVE_REQUEST_HEADROOM
        )
    if bucket == ARCHIVE:
        return settings.MARKETDATA_ARCHIVE_REQUEST_BUDGET
    return settings.MARKETDATA_OTHER_REQUEST_BUDGET


def _window_limit(bucket):
    total = getattr(settings, "MARKETDATA_WINDOW_LIMIT", 1000)
    return max(1, int(total * _WINDOW_SHARE.get(bucket, _WINDOW_SHARE[OTHER])))


def _window_key(bucket):
    return f"quota:window:5m:{bucket}"


def _check_and_record_window(bucket=OTHER):
    """Enforce the bucket's slice of the rolling 5-minute request window."""
    window_limit = _window_limit(bucket)
    window_seconds = getattr(settings, "MARKETDATA_WINDOW_SECONDS", 300)
    now = time.time()
    client = get_redis()
    if client is not None:
        try:
            current_count = client.eval(
                "redis.call('zremrangebyscore', KEYS[1], 0, ARGV[1]); "
                "local count = redis.call('zcard', KEYS[1]); "
                "if count >= tonumber(ARGV[4]) then return -1 end; "
                "redis.call('zadd', KEYS[1], ARGV[2], ARGV[3]); "
                "redis.call('expire', KEYS[1], ARGV[5]); "
                "return count + 1",
                1,
                _window_key(bucket),
                now - window_seconds,
                now,
                uuid.uuid4().hex,
                window_limit,
                window_seconds + 60,
            )
            if current_count < 0:
                raise QuotaExhausted(
                    f"5-minute window limit reached for {bucket} "
                    f"({window_limit}/{window_limit} req)."
                )
            return current_count
        except QuotaExhausted:
            raise
        except Exception:
            logger.warning("[QUOTA] Redis window limiter unavailable for bucket %s.", bucket)

    if bucket == ARCHIVE and getattr(settings, "MARKETDATA_REQUIRE_SHARED_WINDOW", True):
        # A per-process window multiplies the real allowance by the number of
        # workers. Backfill can wait; it must not be the reason live fetches get
        # refused by the provider. Tests run single-process and switch this off.
        raise QuotaExhausted("Shared window limiter unavailable; archive requests paused.")

    # ponytail: degraded per-process window assuming <=10 processes; the shared
    # Redis limiter is the real one, this only keeps live prices trickling.
    local_limit = max(1, window_limit // _DEGRADED_PROCESS_DIVISOR)
    window = _LOCAL_WINDOWS[bucket]
    cutoff = now - window_seconds
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= local_limit:
        raise QuotaExhausted(
            f"Degraded local 5-minute window limit reached for {bucket} "
            f"({len(window)}/{local_limit} req)."
        )
    window.append(now)
    return len(window)


def live_reserve_remaining(row, now=None):
    """Requests to hold back for live prices between now and the day rollover.

    The static floor reserved the same 600 at 23:00 as at 08:00, so the archive
    could never touch the tail of a quiet day. This asks the only question that
    matters: how many requests can live still spend before the quota day ends?

    Cycles left x calls per cycle, capped by what is actually left of the live
    bucket -- live cannot borrow, so reserving beyond its own budget protects
    requests nobody is allowed to make. The cap shrinks to zero at rollover.
    """
    now = now or timezone.now()
    local = now.astimezone(ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE))
    rollover = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # The fastest configured cadence is the worst case: whichever market state
    # the day passes through, live can never need more cycles than this.
    interval = max(1, min(
        settings.MARKETDATA_LIVE_INTERVAL_OPEN,
        settings.MARKETDATA_LIVE_INTERVAL_DAYTIME,
        settings.MARKETDATA_LIVE_INTERVAL_OVERNIGHT,
    ))
    cycles = math.ceil((rollover - local).total_seconds() / interval)
    needed = cycles * settings.MARKETDATA_LIVE_REQUESTS_PER_CYCLE
    return max(0, min(needed, bucket_budget(LIVE) - row.live_used))


def _headroom_for_borrowing(row):
    """Daily requests nobody has claimed and nobody has reserved.

    The three bucket budgets deliberately sum to less than the daily limit, so a
    quiet live day used to leave ~1,000 of the fixed 9,800 unspent -- the archive
    backlog stopped dead at its own 8,800 cap while the account still had room.
    What stays untouchable is live's remaining need for the rest of the day and
    the small `other` allowance for catalog/metadata calls.
    """
    unspent_other = max(0, bucket_budget(OTHER) - row.other_used)
    return row.limit - row.used - live_reserve_remaining(row) - unspent_other


def _may_borrow(bucket, row):
    """Only ARCHIVE borrows: it is the one bucket with an unbounded backlog."""
    return bucket == ARCHIVE and _headroom_for_borrowing(row) > 0


def reserve_request(bucket=OTHER):
    limit = settings.MARKETDATA_DAILY_REQUEST_LIMIT
    budget = bucket_budget(bucket)
    field = f"{bucket}_used"
    with transaction.atomic():
        row, _ = ApiRequestQuota.objects.select_for_update().get_or_create(
            day=quota_day(),
            defaults={"limit": limit},
        )
        if row.limit != limit:
            row.limit = limit
        if row.used >= row.limit:
            raise QuotaExhausted("Daily API request quota exhausted.")
        if getattr(row, field) >= budget and not _may_borrow(bucket, row):
            raise QuotaExhausted(f"Daily {bucket} request budget exhausted ({budget}).")
        if bucket != LIVE:
            # The reserve is enforced against the day total, not just the archive
            # budget, so provider-reconciled drift can never eat it either.
            reserve = live_reserve_remaining(row)
            if row.used + reserve >= row.limit:
                raise QuotaExhausted(
                    f"Remaining daily quota is reserved for live prices "
                    f"({reserve} req to cover the rest of the day)."
                )

        _check_and_record_window(bucket)

        row.used += 1
        setattr(row, field, getattr(row, field) + 1)
        row.save(update_fields=["limit", "used", field, "updated_at"])
        return row.limit - row.used


def reconcile_account(account):
    """Trust the provider's own counters over ours; return its backoff seconds.

    The `account` block only rides along on ERROR responses, so its absence means
    "no news", never zero usage -- treating a missing block as zero would reset the
    day counter on every successful fetch.
    """
    if not isinstance(account, dict):
        return 0
    try:
        block = int(account.get("request_block") or 0)
    except (TypeError, ValueError):
        block = 0
    try:
        usage = int(account["usage_today"])
    except (KeyError, TypeError, ValueError):
        return block

    with transaction.atomic():
        row, _ = ApiRequestQuota.objects.select_for_update().get_or_create(
            day=quota_day(),
            defaults={"limit": settings.MARKETDATA_DAILY_REQUEST_LIMIT},
        )
        if row.used != usage:
            logger.info(
                "[QUOTA] Reconciled day usage from %d to provider-reported %d.",
                row.used,
                usage,
            )
            row.used = usage
            row.save(update_fields=["used", "updated_at"])
    return block


def remaining_requests(bucket=None):
    row = ApiRequestQuota.objects.filter(day=quota_day()).first()
    used = row.used if row else 0
    day_left = settings.MARKETDATA_DAILY_REQUEST_LIMIT - used
    if bucket is None:
        return day_left
    bucket_left = bucket_budget(bucket) - (getattr(row, f"{bucket}_used") if row else 0)
    if row is None:
        row = ApiRequestQuota(
            day=quota_day(), limit=settings.MARKETDATA_DAILY_REQUEST_LIMIT
        )  # unsaved stand-in so the reserve arithmetic reads a zeroed day
    if bucket == ARCHIVE:
        # Mirror _may_borrow, or claim_archive_batch would size batches off the
        # 8,800 cap and hand back an empty batch while the day still had room.
        bucket_left = max(bucket_left, _headroom_for_borrowing(row))
    if bucket != LIVE:
        day_left -= live_reserve_remaining(row)
    return max(0, min(bucket_left, day_left))


def get_quota_status():
    """Detailed quota metrics: daily usage history, remaining quotas, and 5-min window status."""
    day = quota_day()
    limit = settings.MARKETDATA_DAILY_REQUEST_LIMIT
    row = ApiRequestQuota.objects.filter(day=day).first()
    used = row.used if row else 0

    window_seconds = getattr(settings, "MARKETDATA_WINDOW_SECONDS", 300)
    window_limit = getattr(settings, "MARKETDATA_WINDOW_LIMIT", 1000)
    client = get_redis()
    now = time.time()
    window_by_bucket = {}
    for bucket in (LIVE, ARCHIVE, OTHER):
        count = 0
        if client is not None:
            try:
                client.zremrangebyscore(_window_key(bucket), 0, now - window_seconds)
                count = client.zcard(_window_key(bucket))
            except Exception:
                count = len(_LOCAL_WINDOWS[bucket])
        else:
            cutoff = now - window_seconds
            count = sum(1 for ts in _LOCAL_WINDOWS[bucket] if ts >= cutoff)
        window_by_bucket[bucket] = count
    window_used = sum(window_by_bucket.values())

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
        "archive_used": row.archive_used if row else 0,
        "live_used": row.live_used if row else 0,
        "other_used": row.other_used if row else 0,
        "historical_full_used": get_historical_full_used_today(),
        "dynamic_archive_used": max(0, (row.archive_used if row else 0) - get_historical_full_used_today()),
        "archive_budget": bucket_budget(ARCHIVE),
        "live_budget": bucket_budget(LIVE),
        "live_floor": settings.MARKETDATA_LIVE_REQUEST_FLOOR,
        "other_budget": bucket_budget(OTHER),
        "remaining_archive": remaining_requests(ARCHIVE),
        "remaining_live": remaining_requests(LIVE),
        "window_limit": window_limit,
        "window_seconds": window_seconds,
        "window_used": window_used,
        "window_by_bucket": window_by_bucket,
        "remaining_window": max(0, window_limit - window_used),
        "quota_history": history,
    }

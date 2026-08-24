"""Atomic daily and 5-minute window provider quota shared by every web and Celery process.

Two dimensions, and they are not the same thing:

* **plan** -- which provider subscription is billed. BrsApi issues one API key
  per plan and meters each independently: `Tsetmc/*` and `Codal/*` against
  TSETMC_API_KEY, `Market/*` against BRS_API_KEY. These are separate wallets.
* **bucket** -- which lane inside a plan is spending (live prices, archive
  backfill, or incidental metadata). This is our own allocation policy.

Conflating the two is what broke production on 2026-08-24: one shared counter
capped at 9,800 meant a full TSETMC backfill refused gold/currency requests that
still had 79% of the BRS plan free, so the USDT quote failed 201 times in a day
and dollar-denominated holdings went stale.

There is deliberately **no hardcoded daily ceiling**. The provider is the only
authority on how much is left, so we spend until it says stop and then break the
circuit for that plan until the Tehran-midnight reset. Within a plan we still
reserve live's forward-looking need, because that is our policy, not a limit.
"""
import collections
import logging
import re
import time
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from portfolio.live.redis_client import get_redis
from .models import ApiRequestQuota

logger = logging.getLogger(__name__)

ARCHIVE = "archive"
LIVE = "live"
OTHER = "other"

# Provider subscriptions. One counter row and one circuit breaker per plan.
TSETMC = "tsetmc"
BRS = "brs"
PLANS = (TSETMC, BRS)

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


def _seconds_to_rollover(now=None):
    now = now or timezone.now()
    local = now.astimezone(ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE))
    rollover = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60, int((rollover - local).total_seconds()))


# --------------------------------------------------------------- circuit breaker

# What the provider's body looks like when the subscription is spent. Matched on
# the body rather than the status code alone: BrsApi answers quota exhaustion
# with a 5xx, but a plain 5xx is also just a bad minute at the origin, and
# pausing a whole plan for the rest of the day on an ordinary blip would be a
# self-inflicted outage.
_QUOTA_MESSAGE_RE = re.compile(
    r"quota|limit|اعتبار|محدودیت|تعداد درخواست|درخواست.*مجاز", re.I
)


def _breaker_key(plan):
    return f"quota:blocked:{plan}:{quota_day()}"


def is_plan_blocked(plan):
    from django.core.cache import cache

    try:
        return bool(cache.get(_breaker_key(plan)))
    except Exception:
        # Failing open is correct here: the provider still enforces its own
        # limit, and refusing every request because the cache blinked would take
        # live prices down for a reason that has nothing to do with quota.
        return False


def trip_plan_breaker(plan, *, reason=""):
    """Stop spending this plan until the Tehran-midnight reset.

    Called when the provider itself reports exhaustion. Idempotent, and scoped
    to one plan so a spent TSETMC subscription never silences gold/currency.

    Uses the Django cache rather than a raw Redis handle: it is Redis-backed in
    every deployed environment, shared across workers exactly the same way, and
    it works in-process under tests, where a breaker that silently never trips
    would be worse than no test at all.
    """
    from django.core.cache import cache

    try:
        cache.set(_breaker_key(plan), reason or "1", timeout=_seconds_to_rollover())
    except Exception:
        logger.warning("could not persist quota breaker for plan %s", plan)
    logger.warning(
        "quota_breaker_tripped plan=%s reason=%s until=rollover", plan, reason or "-"
    )


def looks_like_quota_error(status_code, body_text):
    """True when this response is the provider saying "you are out"."""
    if status_code not in (429, 500, 502, 503):
        return False
    return bool(_QUOTA_MESSAGE_RE.search(body_text or ""))


def _quota_row(plan, *, locked=False):
    manager = ApiRequestQuota.objects
    if locked:
        manager = manager.select_for_update()
    row, _ = manager.get_or_create(day=quota_day(), plan=plan)
    return row


def bucket_budget(bucket):
    """The bucket's own ceiling, where one exists.

    LIVE and OTHER keep a configured ceiling: both have a bounded, knowable daily
    cost, and a cap that grows with the thing it is capping cannot bind -- a
    mis-set cadence would silently raise its own ceiling with no signal.

    ARCHIVE returns None: **unbounded**. Its backlog is effectively infinite, so
    any number here is arbitrary, and the previous arbitrary number (9,800 minus
    the others) was the bug. What stops the archive is the live reserve inside
    its plan, the rolling window limiter, and ultimately the provider's own
    refusal -- not a constant in a settings file.
    """
    if bucket == LIVE:
        return (
            settings.MARKETDATA_LIVE_REQUEST_FLOOR
            + settings.MARKETDATA_LIVE_REQUEST_HEADROOM
        )
    if bucket == OTHER:
        return settings.MARKETDATA_OTHER_REQUEST_BUDGET
    return None


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
            logger.warning("redis window limiter unavailable for bucket %s", bucket)

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


# Which provider plan each live price-loop job bills. `gold_currency` is the
# BRS wallet; the TSE index probe and the AllSymbols fetch are TSETMC's.
_JOB_PLAN = {"gold_currency": BRS, "market_index": TSETMC, "tsetmc": TSETMC}


def _simulate_price_loop(start, end, plan=None):
    """Requests the 2-minute price loop makes across [start, end).

    Simulating the same state planner the live loop uses is both smaller and more
    accurate than a second formula that drifts whenever endpoint gating changes.
    At most ~720 iterations at the supported cadences.

    `plan` counts only the jobs billed to that subscription.
    """
    from . import market_state as _market_state

    has_brs = bool(getattr(settings, "BRS_API_KEY", ""))
    has_tsetmc = bool(getattr(settings, "TSETMC_API_KEY", ""))
    ignore_hours = getattr(settings, "MARKETDATA_IGNORE_MARKET_HOURS", False)
    intervals = {
        _market_state.OPEN: settings.MARKETDATA_LIVE_INTERVAL_OPEN,
        _market_state.CLOSED_DAYTIME: settings.MARKETDATA_LIVE_INTERVAL_DAYTIME,
        _market_state.OVERNIGHT: settings.MARKETDATA_LIVE_INTERVAL_OVERNIGHT,
    }
    needed = 0
    cursor = start
    next_state_probe = start
    while cursor < end:
        state = _market_state.market_state_at(cursor)
        include_state_probe = (
            has_tsetmc
            and state == _market_state.OPEN
            and cursor >= next_state_probe
        )
        jobs = _market_state.live_job_keys(
            state=state,
            now=cursor,
            has_brs=has_brs,
            has_tsetmc=has_tsetmc,
            ignore_hours=ignore_hours,
            include_state_probe=include_state_probe,
        )
        needed += sum(
            1 for job in jobs
            if plan is None or _JOB_PLAN.get(job, TSETMC) == plan
        )
        if include_state_probe:
            next_state_probe = cursor + timedelta(seconds=_market_state._STATE_TTL_OPEN)
        cursor += timedelta(seconds=max(1, intervals[state]))
    return needed


def live_reserve_remaining(plan, row=None, now=None):
    """Requests to hold back for live on `plan` between now and the day rollover.

    The static floor reserved the same 600 at 23:00 as at 08:00, so the archive
    could never touch the tail of a quiet day. This asks the only question that
    matters: how many requests can live still spend before the quota day ends?

    Two lanes are summed, because both bill the live bucket: the price loop
    (simulated above) and the cadence-driven snapshot endpoints in
    `LiveFetchState`. Pricing only the first is what let crypto, commodity, ETF
    NAV, options and futures spend ~350/day that nothing had reserved for.

    Scoped to one plan: reserving TSETMC's live need out of the BRS wallet is
    exactly the cross-plan confusion this module now exists to prevent.
    """
    now = now or timezone.now()
    local = now.astimezone(ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE))
    rollover = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    needed = _simulate_price_loop(local, rollover, plan=plan)
    try:
        from . import live_states

        needed += live_states.planned_requests(now, rollover, plan=plan)
    except Exception:
        # An unreadable plan must not silently release the reserve.
        # Under-reserving is the failure that gets live requests refused;
        # over-reserving only slows the backfill.
        logger.warning("live plan unavailable; reserving the price loop only")
    if row is not None:
        # Live cannot borrow, so reserving beyond its own ceiling would protect
        # requests nobody is allowed to make. Shrinks to zero at rollover.
        needed = min(needed, bucket_budget(LIVE) - row.live_used)
    return max(0, needed)


def reserve_request(bucket=OTHER, plan=TSETMC):
    """Claim one request on `plan` for `bucket`, or raise `QuotaExhausted`.

    Three gates, and deliberately no daily ceiling:

    1. The plan's circuit breaker -- the provider already told us it is spent.
    2. The bucket's own ceiling, where it has one (ARCHIVE does not).
    3. Live's forward-looking reserve, so backfill cannot eat the requests the
       price loop still needs before rollover. Enforced only against non-live
       buckets, within this plan.
    """
    if is_plan_blocked(plan):
        raise QuotaExhausted(
            f"Provider reported the {plan} plan exhausted; paused until reset."
        )
    budget = bucket_budget(bucket)
    field = f"{bucket}_used"
    with transaction.atomic():
        row = _quota_row(plan, locked=True)
        if budget is not None and getattr(row, field) >= budget:
            raise QuotaExhausted(
                f"Daily {bucket} request budget exhausted ({budget}) on {plan}."
            )
        if bucket != LIVE and row.limit:
            # `limit` is only set once the provider has told us what this plan's
            # ceiling actually is (see `reconcile_account`). Until then there is
            # nothing to reserve *against*, and the breaker is the real stop.
            reserve = live_reserve_remaining(plan, row)
            if row.used + reserve >= row.limit:
                raise QuotaExhausted(
                    f"Remaining {plan} quota is reserved for live prices "
                    f"({reserve} req to cover the rest of the day)."
                )

        _check_and_record_window(bucket)

        row.used += 1
        setattr(row, field, getattr(row, field) + 1)
        row.save(update_fields=["used", field, "updated_at"])
        return (row.limit - row.used) if row.limit else None


def reconcile_account(account, plan=TSETMC):
    """Conservatively merge one plan's provider counters; return its backoff seconds.

    The `account` block only rides along on ERROR responses, so its absence means
    "no news", never zero usage -- treating a missing block as zero would reset the
    day counter on every successful fetch.

    Scoped to `plan`. Merging both subscriptions into one row meant the larger
    counter always won: BRS spend was invisible in `ApiRequestQuota` all the way
    through the incident, which is why the shared total looked plausible while
    one wallet sat 79% unused.
    """
    if not isinstance(account, dict):
        return 0
    try:
        block = int(account.get("request_block") or 0)
    except (TypeError, ValueError):
        block = 0

    # The provider is also the only authority on this plan's ceiling. Recording
    # what it reports is what makes the limit observed data instead of a guess
    # in a settings file.
    reported_limit = None
    for name in ("limit_today", "daily_limit", "request_limit", "limit"):
        try:
            reported_limit = int(account[name])
            break
        except (KeyError, TypeError, ValueError):
            continue

    try:
        usage = int(account["usage_today"])
    except (KeyError, TypeError, ValueError):
        usage = None

    if usage is None and reported_limit is None:
        return block

    with transaction.atomic():
        row = _quota_row(plan, locked=True)
        updates = []
        # Responses from concurrent requests can arrive out of order. Lowering
        # the local counter to an older response re-opens quota that was already
        # spent, while a larger counter is useful evidence of calls made outside
        # this process. Keep usage monotonic and account unexplained positive
        # drift in `other_used` so the bucket sum remains an exact ledger.
        if usage is not None and usage > row.used:
            drift = usage - row.used
            logger.info(
                "quota reconciled plan=%s day_usage %d->%d unattributed=%d",
                plan, row.used, usage, drift,
            )
            row.used = usage
            row.other_used += drift
            updates += ["used", "other_used"]
        if reported_limit and reported_limit != row.limit:
            logger.info(
                "quota limit observed plan=%s %d->%d", plan, row.limit, reported_limit
            )
            row.limit = reported_limit
            updates.append("limit")
        if updates:
            row.save(update_fields=[*updates, "updated_at"])
    return block


#: What `claim_archive_batch` may assume is available on a plan whose ceiling the
#: provider has not disclosed yet. Only ever bounds ONE batch -- the breaker and
#: the 5-minute window are the real stops -- so it needs to be big enough not to
#: throttle a healthy day, not accurate.
_UNKNOWN_LIMIT_BATCH_ALLOWANCE = 10_000


def remaining_requests(bucket=None, plan=TSETMC):
    """How many requests `bucket` may still spend on `plan` right now.

    With no hardcoded daily limit this is an estimate, not a contract: it sizes
    archive batches and feeds the Ops console. The authoritative "stop" is the
    provider's own refusal, via `trip_plan_breaker`.
    """
    if is_plan_blocked(plan):
        return 0
    row = ApiRequestQuota.objects.filter(day=quota_day(), plan=plan).first()
    used = row.used if row else 0
    limit = (row.limit if row and row.limit else 0) or _UNKNOWN_LIMIT_BATCH_ALLOWANCE
    day_left = max(0, limit - used)
    if bucket is None:
        return day_left
    budget = bucket_budget(bucket)
    if budget is not None:
        day_left = min(
            day_left, max(0, budget - (getattr(row, f"{bucket}_used") if row else 0))
        )
    if bucket != LIVE:
        day_left -= live_reserve_remaining(plan, row)
    return max(0, day_left)


def archive_capacity():
    """Archive room across every plan, as `{plan: remaining}`.

    A single archive batch is a mixed bag -- `gold_daily` bills BRS while every
    stock endpoint bills TSETMC -- so the scheduler asks about all wallets at
    once and lets the per-request reserve refuse the individual calls. Summing
    to one number here is what would reintroduce the original bug.
    """
    return {plan: remaining_requests(ARCHIVE, plan) for plan in PLANS}


def get_quota_status():
    """Detailed quota metrics: per-plan usage, remaining quotas, and window status."""
    day = quota_day()
    rows = {
        row.plan: row
        for row in ApiRequestQuota.objects.filter(day=day)
    }
    plans = {}
    for plan in PLANS:
        row = rows.get(plan)
        plans[plan] = {
            "plan": plan,
            # 0 means "the provider has not told us yet"; the UI shows it as
            # unknown rather than inventing a number.
            "limit": row.limit if row else 0,
            "used": row.used if row else 0,
            "archive_used": row.archive_used if row else 0,
            "live_used": row.live_used if row else 0,
            "other_used": row.other_used if row else 0,
            "blocked": is_plan_blocked(plan),
            "remaining_archive": remaining_requests(ARCHIVE, plan),
            "remaining_live": remaining_requests(LIVE, plan),
            "live_reserve": live_reserve_remaining(plan, row),
        }
    used = sum(entry["used"] for entry in plans.values())
    limit = sum(entry["limit"] for entry in plans.values())

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
        ApiRequestQuota.objects.all().order_by("-day", "plan")[:60].values(
            "day", "plan", "limit", "used", "archive_used", "live_used",
            "other_used", "updated_at",
        )
    )
    for h in history:
        h["day"] = str(h["day"])
        h["updated_at"] = h["updated_at"].isoformat() if h["updated_at"] else ""

    archive_used = sum(entry["archive_used"] for entry in plans.values())
    historical_full = get_historical_full_used_today()
    return {
        "day": str(day),
        "plans": plans,
        # Cross-plan totals. Kept because the Ops header and the health check
        # read them, but the per-plan block above is the one that means anything
        # -- these two wallets are not fungible.
        "limit": limit,
        "used": used,
        "remaining_daily": max(0, limit - used) if limit else None,
        "archive_used": archive_used,
        "live_used": sum(entry["live_used"] for entry in plans.values()),
        "other_used": sum(entry["other_used"] for entry in plans.values()),
        "historical_full_used": historical_full,
        "dynamic_archive_used": max(0, archive_used - historical_full),
        "archive_budget": bucket_budget(ARCHIVE),
        "live_budget": bucket_budget(LIVE),
        "live_floor": settings.MARKETDATA_LIVE_REQUEST_FLOOR,
        "other_budget": bucket_budget(OTHER),
        "remaining_archive": sum(
            entry["remaining_archive"] for entry in plans.values()
        ),
        "remaining_live": sum(entry["remaining_live"] for entry in plans.values()),
        "window_limit": window_limit,
        "window_seconds": window_seconds,
        "window_used": window_used,
        "window_by_bucket": window_by_bucket,
        "remaining_window": max(0, window_limit - window_used),
        "quota_history": history,
    }

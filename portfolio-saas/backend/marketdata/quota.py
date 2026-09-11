"""Atomic daily and 5-minute window provider quota shared by every web and Celery process.

Two dimensions, and they are not the same thing:

* **plan** -- which provider meter is billed. BrsApi bills **one API key against
  independent per-product meters, chosen by the endpoint's path family**.
  Measured directly against the live API on 2026-09-09 by probing each path and
  watching which counter moved:

  ===========================  =========================  ==================
  Path family                  Product                    Daily ceiling
  ===========================  =========================  ==================
  ``Tsetmc/*``, ``Codal/*``    purchased (خرید شده)       **10,000**
  ``Market/Gold_Currency_Pro`` NOT purchased (خرید نشده)  0 -- free tier
  other ``Market/*``           free                       not metered at all
  ===========================  =========================  ==================

  So there is exactly **one paid wallet**. The 1,500/day gold/FX/crypto plan
  this file assumed until 2026-09-09 had lapsed: the provider reports that
  product as `خرید نشده` with `exp: null`, and a hit on `Gold_Currency.php`,
  `Cryptocurrency.php` or `Commodity.php` moves no counter at all. We were
  reserving 1,200 of a phantom 1,500 for live and leaving gold backfill 150
  requests a day, to protect a meter that does not exist.

  The key is therefore **not** the thing that selects the plan, and the two env
  vars deliberately hold the same value in production. Earlier comments in this
  file claimed "one API key per plan"; that was wrong and actively misleading --
  someone reading it would conclude the deployment was misconfigured and "fix"
  it by inventing a second key. `endpoints.Endpoint.plan` is the only correct
  mapping, because only the path decides which meter the provider debits.

  A plan with an effective limit of 0 is **unmetered**, not empty. That is the
  provider's own semantics (`usage_today_limit: 0` on a free product), so an
  unmetered plan gets no daily ceiling and no live reserve -- only the rolling
  5-minute window shapes it.
* **bucket** -- which lane inside a plan is spending (live prices, archive
  backfill, or incidental metadata). This is our own allocation policy.

Conflating the two is what broke production on 2026-08-24: one shared counter
capped at 9,800 meant a full TSETMC backfill refused gold/currency requests that
still had 79% of the BRS plan free, so the USDT quote failed 201 times in a day
and dollar-denominated holdings went stale.

The allocation policy, in one line: **live keeps what it still needs, archive
gets the rest.**

* **Live** reserves the requests its cadence will actually make *between now and
  Tehran midnight*, not over a whole day. The reserve was static until
  2026-09-09, which meant that at 13:00 -- when the TSE session ends and the
  live lane's remaining need really is near zero -- 1,074 TSETMC requests stayed
  fenced off for eleven more hours and expired unspent every single evening.
  Forward-looking is not the 2026-08-26 bug returning: what starved live then
  was gate 3 of `reserve_request` being skipped entirely whenever the provider
  had not disclosed a limit, and that gate is unconditional now.
* **Archive** gets whatever is left, minus a safety margin. Backfill is expected
  to be slow and to lag by days or weeks; that is the design.

The provider remains the only authority on how much is really left, and its
refusal still breaks the circuit for that plan until the Tehran-midnight reset.
`effective_limit` falls back to a configured per-plan expectation only until a
real answer arrives.

**Getting that real answer is deliberate, not incidental.** The `account` block
does not ride on a successful response -- verified 2026-09-09 -- so the counter
used to drift unobserved between error responses, by 2,226 requests on
2026-09-08. `fetchers.probe_meter` buys the block for one request, and
`tasks.reconcile_quota_meters` does that every five minutes while a plan is
spending. Roughly 1-3% of the wallet buys a counter that matches the vendor
panel, which is what makes the rest of this file's arithmetic trustworthy
instead of merely plausible.

Do not replace that with a correction factor. One existed here between
2026-09-08 and 2026-09-09 -- archive's leftover divided by 1.30, to "account
for" the 2,225-request gap -- and it silently discarded 1,950 TSETMC requests a
day. The gap it was calibrated against was mostly an operator hand-probing the
API from the VPS while debugging that very incident.

Note the per-plan expectation is *not* the 2026-08-24 bug returning. That was one
**shared** counter across two wallets, so spending either drained both. These are
per-plan, and they bound our own reservation maths, never the provider's answer.
"""
import collections
import logging
import re
import threading
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


# Why a claim was refused. These are not interchangeable, and treating them as
# one thing is what stalled the archive for eight days (see PACING_REASONS).
REASON_PLAN_BLOCKED = "plan_blocked"
REASON_BUCKET_EXHAUSTED = "bucket_exhausted"
REASON_LIVE_RESERVED = "live_reserved"
REASON_ARCHIVE_PACED = "archive_paced"

#: Refusals that mean "wait, and try again shortly" rather than "the wallet is
#: spent". The distinction is the whole point:
#:
#: `archive_paced` says the archive is ahead of its pro-rata share *at this
#: instant*. The condition clears on its own within minutes as the day advances.
#:
#: `live_reserved` **joined this set on 2026-09-09**, and only because
#: `live_reserve_remaining` became forward-looking on the same day. While the
#: reserve was a static 24h figure the gate expanded to
#: `archive_used + other_used + live_day_cost` -- the `live_used` terms cancel,
#: so it could not move before rollover and retrying it would have spun for the
#: rest of the day. Now the reserve shrinks as the remaining cadence shrinks,
#: and the single largest release happens at 13:00 Tehran when the TSE session
#: ends. A state parked until tomorrow at 10:00 would sit out an afternoon that
#: has room for it. If the reserve is ever made static again, take
#: `live_reserved` back out of this set in the same commit.
#:
#: `plan_blocked` and `bucket_exhausted` remain day-scoped.
PACING_REASONS = frozenset({REASON_ARCHIVE_PACED, REASON_LIVE_RESERVED})


class QuotaExhausted(RuntimeError):
    """Raised when a claim is refused. `reason` is the ledger grouping key.

    Callers that reschedule work MUST branch on `reason`. `run_archive_state`
    did not, and deferred every refusal to the next quota day -- so a pacing
    wait measured in minutes parked the state for 24 hours. Because nearly the
    whole backlog falls due at Tehran midnight, and midnight is exactly when the
    paced allowance sits at its floor, this parked ~7,000 states nightly and left
    ~4,000 TSETMC requests a day unspent from 2026-08-27 to 2026-09-04.
    """

    def __init__(self, message, reason="quota_exhausted"):
        super().__init__(message)
        self.reason = reason

    @property
    def is_pacing(self):
        """True when this refusal clears on its own within minutes."""
        return self.reason in PACING_REASONS


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


def _probe_key(plan):
    return f"{_breaker_key(plan)}:probe"


def _probe_claimed(plan):
    from django.core.cache import cache

    try:
        return bool(cache.get(_probe_key(plan)))
    except Exception:
        return False


def _claim_breaker_probe(plan):
    """Atomically admit the one half-open request. False if someone else holds it."""
    interval = int(getattr(settings, "MARKETDATA_BREAKER_RETRY_SECONDS", 900) or 0)
    if interval <= 0:
        return False
    from django.core.cache import cache

    try:
        return cache.add(_probe_key(plan), 1, timeout=interval)
    except Exception:
        return False


def clear_plan_breaker(plan, *, bucket=None):
    """Drop the trip only if this request was allowed to ask the provider.

    A live 200 during an archive-triggered trip means the reserved live slice
    still works, not that archive should resume hammering a wallet the provider
    just refused. Clearing on every parseable JSON was the opposite of
    bucket-aware: live would wipe an archive trip on the next 60s poll.
    """
    from django.core.cache import cache

    try:
        tripped = cache.get(_breaker_key(plan))
        if not tripped:
            return
        # A success already in flight can finish after another request proves
        # the daily wallet is empty. It must not reopen a meter that only the
        # provider's day rollover can refill.
        if isinstance(tripped, dict) and tripped.get("until_reset"):
            return
        tripped_by = tripped.get("bucket") if isinstance(tripped, dict) else None
        if not _bucket_may_clear(tripped_by, bucket):
            return
        cache.delete(_breaker_key(plan))
        cache.delete(_probe_key(plan))
    except Exception:
        logger.warning("could not clear quota breaker for plan %s", plan)


def _bucket_may_clear(tripped_by, bucket):
    if bucket is None:
        return True
    if tripped_by is None:
        return True
    if tripped_by == LIVE:
        return bucket == LIVE
    return bucket != LIVE


def is_plan_blocked(plan, bucket=None, *, admit=False, holding_probe=False):
    """Whether `bucket` is currently barred from spending `plan`.

    `bucket=None` asks the plan-wide question, which is what the Ops console and
    the health check want. Passing a bucket asks the narrower one.

    An ARCHIVE-triggered trip does not stop LIVE. Archive is now hard-capped
    short of the ceiling, so it should never be able to exhaust a plan at all --
    but on 2026-08-26 it did, the breaker fired, and because this gate ran before
    any bucket distinction it took the live lane down with it for the rest of the
    day (live_used=0 against archive_used=10,034). Live has its own reserved
    headroom; it should not be collateral damage for archive overrunning.

    A LIVE-triggered trip still stops live: that is the provider refusing live
    itself, and hammering it further only deepens the hole.

    Ambiguous quota-shaped errors are HALF-OPEN after
    `MARKETDATA_BREAKER_RETRY_SECONDS`; a response whose account meter proves
    the daily wallet empty is latched until rollover. This keeps an ordinary
    origin limit error from costing a whole day while preventing pointless
    probes against a known-empty wallet.
    """
    from django.core.cache import cache

    try:
        tripped = cache.get(_breaker_key(plan))
    except Exception:
        # Failing open is correct here: the provider still enforces its own
        # limit, and refusing every request because the cache blinked would take
        # live prices down for a reason that has nothing to do with quota.
        return False
    if not tripped:
        return False
    # Values written before this became bucket-aware are bare strings; treat
    # them as plan-wide so a rollover mid-deploy can only ever fail safe.
    tripped_by = tripped.get("bucket") if isinstance(tripped, dict) else None
    if bucket == LIVE and tripped_by is not None and tripped_by != LIVE:
        return False
    if _breaker_retry_due(tripped):
        # A LIVE-ranked trip is probed only by live. Archive stealing the one
        # slot would spend it, re-trip, and freeze quotes for another interval.
        if tripped_by == LIVE and bucket != LIVE:
            return True
        if holding_probe:
            return False
        if admit:
            return not _claim_breaker_probe(plan)
        return _probe_claimed(plan)
    return True


def _breaker_retry_due(tripped, now=None):
    """Whether a tripped breaker is due to let one request through.

    A payload with no `tripped_at` predates half-open (or is a legacy bare
    string). Those stay closed until rollover rather than reopening en masse the
    moment this deploys.
    """
    if not isinstance(tripped, dict):
        return False
    if tripped.get("until_reset"):
        return False
    tripped_at = tripped.get("tripped_at")
    if not tripped_at:
        return False
    interval = int(getattr(settings, "MARKETDATA_BREAKER_RETRY_SECONDS", 900) or 0)
    if interval <= 0:
        return False
    clock = time.time() if now is None else now
    return clock - tripped_at >= interval


def _breaker_is_half_open(plan, bucket=None):
    """Peek: tripped, due to probe, and this bucket is the one allowed to probe."""
    from django.core.cache import cache

    try:
        tripped = cache.get(_breaker_key(plan))
    except Exception:
        return False
    if not tripped:
        return False
    tripped_by = tripped.get("bucket") if isinstance(tripped, dict) else None
    if bucket == LIVE and tripped_by is not None and tripped_by != LIVE:
        return False
    if tripped_by == LIVE and bucket != LIVE:
        return False
    return _breaker_retry_due(tripped) and not _probe_claimed(plan)


def trip_plan_breaker(plan, *, reason="", bucket=None, until_reset=False):
    """Stop spending this plan until the next half-open probe, or rollover.

    `until_reset` is reserved for provider meter evidence that the daily wallet
    is empty. The caller broadens that trip to every bucket, and this flag
    disables half-open probes until the Tehran-day cache key expires.

    Re-tripping always refreshes `tripped_at` while KEEPING the broadest bucket
    seen. Those two rules pull in opposite directions and both are load-bearing:
    the old early `return` on an equal-or-broader existing trip meant a re-trip
    could not re-arm the half-open timer, so a genuinely spent plan would be
    re-probed every interval forever; dropping the rank check instead would let
    an archive trip arriving after a live trip reopen live on a wallet the
    provider had already refused.

    Uses the Django cache rather than a raw Redis handle: it is Redis-backed in
    every deployed environment, shared across workers exactly the same way, and
    it works in-process under tests, where a breaker that silently never trips
    would be worse than no test at all.
    """
    from django.core.cache import cache

    def _rank(tripped_by):
        # None = plan-wide (blocks every bucket, including live). Do not default
        # to OTHER: that would fail-open the live lane for any caller that omits
        # bucket.
        if tripped_by is None:
            return 3
        if tripped_by == LIVE:
            return 2
        return 1

    effective = bucket
    latched = until_reset
    try:
        existing = cache.get(_breaker_key(plan))
        if existing:
            existing_bucket = (
                existing.get("bucket") if isinstance(existing, dict) else None
            )
            if _rank(existing_bucket) >= _rank(bucket):
                effective = existing_bucket
            latched = latched or (
                isinstance(existing, dict) and existing.get("until_reset", False)
            )
        cache.set(
            _breaker_key(plan),
            {
                "bucket": effective,
                "reason": reason or "1",
                "tripped_at": time.time(),
                "until_reset": latched,
            },
            timeout=_seconds_to_rollover(),
        )
        cache.delete(_probe_key(plan))
    except Exception:
        logger.warning("could not persist quota breaker for plan %s", plan)
    logger.warning(
        "quota_breaker_tripped plan=%s bucket=%s reason=%s retry=%s",
        plan, effective or "plan", reason or "-",
        (
            "rollover"
            if latched
            else f"{getattr(settings, 'MARKETDATA_BREAKER_RETRY_SECONDS', 900)}s"
        ),
    )


def looks_like_quota_error(status_code, body_text):
    """True when this response is the provider saying "you are out"."""
    if status_code not in (429, 500, 502, 503):
        return False
    return bool(_QUOTA_MESSAGE_RE.search(body_text or ""))


#: The provider's own name for this plan's daily ceiling, in the order we trust
#: them. `usage_today_limit` is the one BrsApi actually sends -- verified against
#: the live API on 2026-09-09, where a deliberately malformed request returned
#: `{"usage_today": 6929, "usage_today_limit": 10000, ...}`.
#:
#: It was missing from this list for the whole life of the file, so no response
#: ever disclosed a ceiling, `ApiRequestQuota.limit` was 0 on every row ever
#: written, and `effective_limit` always fell back to the configured guess. Every
#: downstream "the provider has not told us yet" comment described a field-name
#: typo, not the provider.
_LIMIT_FIELDS = ("usage_today_limit", "limit_today", "daily_limit", "request_limit", "limit")


def reported_limit(account):
    """This plan's ceiling as the provider states it, or None if it did not.

    Zero is a real answer and is NOT None: BrsApi reports `usage_today_limit: 0`
    together with `type: "خرید نشده"` (not purchased) for a product on the free
    tier, which is exactly the Market/* family here. Collapsing that into "no
    news" is what would put a phantom ceiling back on an unmetered plan.
    """
    if not isinstance(account, dict):
        return None
    for name in _LIMIT_FIELDS:
        try:
            return int(account[name])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def reported_usage(account):
    """This plan's spend so far as the provider states it, or None."""
    if not isinstance(account, dict):
        return None
    try:
        return int(account["usage_today"])
    except (KeyError, TypeError, ValueError):
        return None


def is_daily_quota_exhaustion(account, plan, row=None):
    """True when the error means the daily wallet is spent, not a burst 429.

    On 2026-09-08 BRS tripped on HTTP 429 with 77 archive calls logged while the
    vendor panel still showed 349/1500. TSETMC the same morning was a real
    empty wallet (10058/10000). `looks_like_quota_error` cannot tell those
    apart; `usage_today` vs the plan ceiling can.
    """
    if not isinstance(account, dict):
        return True
    usage = reported_usage(account)
    if usage is None:
        return True
    limit = reported_limit(account) or effective_limit(plan, row)
    if not limit:
        return True
    return usage >= limit - _safety_margin()


def is_confirmed_daily_quota_exhaustion(account, plan, row=None):
    """Whether provider meter data proves that today's wallet is spent."""
    if not isinstance(account, dict):
        return False
    usage = reported_usage(account)
    if usage is None:
        return False
    limit = reported_limit(account) or effective_limit(plan, row)
    return bool(limit) and usage >= limit - _safety_margin()


def _quota_row(plan, *, locked=False):
    manager = ApiRequestQuota.objects
    if locked:
        manager = manager.select_for_update()
    row, _ = manager.get_or_create(day=quota_day(), plan=plan)
    return row


_PLAN_LIMIT_SETTING = {
    TSETMC: "MARKETDATA_PLAN_LIMIT_TSETMC",
    BRS: "MARKETDATA_PLAN_LIMIT_BRS",
}


def _safety_margin():
    return int(getattr(settings, "MARKETDATA_PLAN_SAFETY_MARGIN", 0) or 0)


def effective_limit(plan, row=None):
    """This plan's working ceiling: what the provider said, else what we expect.

    The provider's disclosed `limit` always wins. Until it arrives we fall back
    to the configured expectation, because a reserve measured against an unknown
    ceiling is not a reserve at all. `limit` only rides along on *error*
    responses, so it is 0 on most days -- and the old `and row.limit` guard meant
    that on those days the live reserve simply never ran and archive was
    completely unthrottled.
    """
    disclosed = getattr(row, "limit", 0) or 0
    if disclosed:
        return disclosed
    name = _PLAN_LIMIT_SETTING.get(plan)
    return int(getattr(settings, name, 0) or 0) if name else 0


def bucket_budget(bucket, plan=TSETMC, row=None):
    """The bucket's own ceiling on `plan`, where one exists.

    LIVE and OTHER keep a configured ceiling: both have a bounded, knowable daily
    cost, and a cap that grows with the thing it is capping cannot bind -- a
    mis-set cadence would silently raise its own ceiling with no signal.

    LIVE's is clamped to the plan it is being spent on. The flat
    FLOOR+HEADROOM (1,700) was applied to every plan alike, but BRS's whole
    wallet is 1,500 -- so live's "budget" there exceeded the subscription and
    could never bind on the thing it was supposed to bound.

    ARCHIVE returns None: **unbounded** as a bucket. Its backlog is effectively
    infinite, so any number here is arbitrary, and the previous arbitrary number
    (9,800 minus the others) was the bug. What stops the archive is the live
    reserve inside its plan, the paced day ceiling, the rolling window limiter,
    and ultimately the provider's own refusal -- not a constant in a settings file.
    """
    if bucket == LIVE:
        configured = (
            settings.MARKETDATA_LIVE_REQUEST_FLOOR
            + settings.MARKETDATA_LIVE_REQUEST_HEADROOM
        )
        ceiling = effective_limit(plan, row)
        if ceiling:
            configured = min(configured, max(0, ceiling - _safety_margin()))
        return configured
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


def live_day_cost(plan, row=None, now=None):
    """Static 24h live spend for this wallet, from Tehran midnight to the next.

    Archive leftover is this number subtracted from the plan ceiling, paced
    across the day. The slice does not shrink after the session closes: unused
    live headroom is not lent to backfill until the next quota day.

    Static within a day, but NOT the same every day: on a Thursday or Friday the
    TSE lane costs nothing because there is no session to poll, which is why
    `now` selects the day being priced instead of the wall clock always winning.
    """
    from . import endpoints, live_states
    from .models import LiveFetchState

    start = live_states.day_start(now)
    needed = _simulate_price_loop(start, start + timedelta(days=1), plan=plan)
    try:
        keys = [key for key, ep in endpoints.REGISTRY.items() if ep.plan == plan]
        states = list(
            LiveFetchState.objects.filter(enabled=True, endpoint_key__in=keys)
        )
        needed += live_states.full_day_cost(states)
    except Exception:
        logger.warning("live plan unavailable; reserving the price loop only")
    # No floor here. `MARKETDATA_LIVE_REQUEST_FLOOR` used to be applied as a
    # lower bound on this number, which made the day cost read 1,200 for a lane
    # whose planner simulates to ~300 and whose measured spend is 114-191. That
    # was a response to the 2026-09-08 429 at 10,058/10,000, but the cause there
    # was the 2,226 requests the counter could not see, not an under-sized
    # reserve -- and `reconcile_quota_meters` addresses that directly now. The
    # floor that remains lives on the *reserve*
    # (`MARKETDATA_LIVE_RESERVE_MIN`), which is the quantity a floor makes sense
    # on, and FLOOR keeps its other job as the basis of `bucket_budget(LIVE)`.
    cap = bucket_budget(LIVE, plan, row)
    if cap is not None:
        needed = min(needed, cap)
    return max(0, needed)


def live_reserve_remaining(plan, row=None, now=None):
    """What the live lane still has to spend on `plan` before Tehran midnight.

    **Forward-looking since 2026-09-09.** It used to be the unused portion of a
    static 24h slice, which does not describe the thing being reserved: the
    price loop cannot retroactively spend the morning's requests, so at 17:00 on
    a closed market the honest answer is "almost nothing" and the static answer
    was 1,074. Those requests were fenced off from backfill and then expired at
    midnight, every day.

    Sized by simulating the same planner the live loop runs (`_simulate_price_loop`)
    plus the cadence-driven `LiveFetchState` rows, over `[now, rollover)`, capped
    by the full day's cost.

    Unmetered plans reserve nothing: there is no wallet to protect live's share
    of, and holding one back only throttles a lane the provider is not counting.
    """
    if not effective_limit(plan, row):
        return 0

    from . import endpoints, live_states
    from .models import LiveFetchState

    now = now or timezone.now()
    end = live_states.day_start(now) + timedelta(days=1)
    needed = _simulate_price_loop(now, end, plan=plan)
    try:
        keys = [key for key, ep in endpoints.REGISTRY.items() if ep.plan == plan]
        states = list(
            LiveFetchState.objects.filter(enabled=True, endpoint_key__in=keys)
        )
        needed += live_states.remaining_day_cost(states, now=now)
    except Exception:
        logger.warning("live plan unavailable; reserving the price loop only")

    return max(0, min(needed, live_day_cost(plan, row, now=now)))


def other_reserve_remaining(plan, row=None, now=None):
    """Unused portion of the OTHER bucket's daily budget on `plan`.

    The exact counterpart of `live_reserve_remaining`, and it exists for the
    same reason. OTHER already has a ceiling (`MARKETDATA_OTHER_REQUEST_BUDGET`,
    200/day) but nothing ever held that budget FOR it: `archive_day_ceiling`
    subtracted only what OTHER had already spent, so under burst allocation
    archive reached the live-reserve line hours before the day's maintenance
    jobs woke up, and every one of them was refused `live_reserved` on its first
    request.

    Those jobs are small, fixed-cost and are the ones that keep the universe
    honest -- `catalog_sync` (03:40 Tehran) failed outright on 2026-09-07, and
    `sync_symbol_metadata` (09:30 Tehran) stopped with 923 symbols never
    fetched. Both are written to resume where they stopped, so a guaranteed
    200/day converges; zero per day never does. The cost to backfill is 2% of
    the TSETMC wallet. BRS has no OTHER endpoints -- catalog and metadata are
    Tsetmc/* -- so holding 200 on that plan would just shrink gold/FX backfill.
    """
    if plan != TSETMC:
        return 0
    local = (now or timezone.now()).astimezone(
        ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE)
    )
    if local.hour >= 10:
        return 0
    spent = getattr(row, "other_used", 0) or 0
    return max(0, (bucket_budget(OTHER, plan, row) or 0) - spent)


def _day_elapsed_fraction(now=None):
    """How far through the archive's *spending* window we are, in [0, 1].

    Not the same as how far through the day we are. The window closes at
    `MARKETDATA_ARCHIVE_PACE_FULL_BY_HOUR` rather than at midnight, because a
    curve that only reaches 100% at 23:59:59 can never actually be consumed --
    the archive would have to spend its last thousand requests in the final
    second. Ending the ramp a few hours early leaves real time to finish the day,
    and the leftover hours act as catch-up room after any stall.
    """
    now = now or timezone.now()
    local = now.astimezone(ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    full_by = max(1, min(24, int(getattr(settings, "MARKETDATA_ARCHIVE_PACE_FULL_BY_HOUR", 24))))
    return min(1.0, max(0.0, (local - start).total_seconds() / (full_by * 3600.0)))


def archive_day_ceiling(plan, row=None, now=None):
    """Total requests ARCHIVE may spend on `plan` across the whole quota day.

    Live is subtracted first and is never negotiable: what its cadence will
    still cost between now and rollover (`live_reserve_remaining`). The OTHER
    bucket's small fixed budget is held back the same way -- see
    `other_reserve_remaining`. Whatever remains, minus a safety margin so the
    wallet is never actually emptied, belongs to backfill.

    Because the live reserve now shrinks through the day, this ceiling *grows*
    through the day. That is the intended shape: an evening with no session left
    to poll should hand the backfill everything the price loop no longer needs,
    instead of letting it expire at midnight.

    None means **unmetered**: the provider is not counting this plan, so there is
    no ceiling to divide up and only the rolling window shapes the rate.

    The subtrahend is `row.used`, not `archive_used`. Everything the provider has
    billed today counts against the same wallet, including the drift that no
    bucket of ours claims (`unattributed_used`) -- charging archive only for its
    own lane is how a plan reaches its real ceiling while our arithmetic still
    reports room.
    """
    ceiling = effective_limit(plan, row)
    if not ceiling:
        return None
    spent_elsewhere = max(0, (getattr(row, "used", 0) or 0) - (getattr(row, "archive_used", 0) or 0))
    reserve = live_reserve_remaining(plan, row, now=now)
    other_reserve = other_reserve_remaining(plan, row, now=now)
    return max(0, ceiling - _safety_margin() - spent_elsewhere - reserve - other_reserve)


def archive_allowance_now(plan, row=None, now=None):
    """How much of the day's archive ceiling may have been spent *by now*.

    **Burst by default** (2026-09-06): the whole ceiling is available from
    00:01, so backfill runs flat out in priority order until the wallet reaches
    the live reserve line and then idles until the next quota day.

    This deliberately reverses the pro-rata curve added after 2026-08-26, when
    archive drained the TSETMC wallet before dawn (~2,750 req/hour from 00:00,
    breaker tripped 03:43, then twenty hours dark with `live_used=0`). The curve
    was the wrong fix for that incident. What actually starved live was that the
    live reserve gate in `reserve_request` was skipped whenever the provider had
    not disclosed a limit -- which is most days. That gate is now unconditional
    and sized per plan and per market state, so live's share is subtracted
    inside `archive_day_ceiling` *before* archive sees a budget at all. Spending
    it early no longer takes anything from live; it only decides whether the
    warehouse converges in weeks or in days.

    Pacing remains available for a plan whose provider punishes bursts: set
    `MARKETDATA_ARCHIVE_PACE_ENABLED=1`. Leave it off unless a provider
    complains -- with it on, the 5-minute window limiter is the only thing
    shaping the rate, which is what we want.
    """
    ceiling = archive_day_ceiling(plan, row, now=now)
    if ceiling is None:
        return None
    if not getattr(settings, "MARKETDATA_ARCHIVE_PACE_ENABLED", False):
        return ceiling
    paced = int(ceiling * _day_elapsed_fraction(now))
    # Never a hard zero immediately after the reset: pro rata at 00:01 is a tiny
    # fraction of the day, which would stall backfill outright for the first
    # minutes and makes the gate depend on wall-clock time in tests.
    #
    # The floor is one hour's worth of the ramp, not one batch. A single batch
    # (120) against a ~9,500 ceiling meant the first minutes of every quota day
    # admitted 120 requests and refused everything else -- and since the refusal
    # used to park each state for 24h, those refusals were the whole day's
    # outcome. An hour of headroom costs nothing (the 5-minute window limiter
    # still caps the rate) and lets the day start at full speed.
    full_by = max(1, min(24, int(getattr(settings, "MARKETDATA_ARCHIVE_PACE_FULL_BY_HOUR", 24))))
    floor = max(
        int(getattr(settings, "MARKETDATA_ARCHIVE_BATCH_SIZE", 0) or 0),
        ceiling // full_by,
    )
    return min(ceiling, max(paced, floor))


def reserve_request(bucket=OTHER, plan=TSETMC, *, holding_probe=False):
    """Claim one request on `plan` for `bucket`, or raise `QuotaExhausted`.

    Gates, in order:

    1. The plan's circuit breaker -- the provider already told us it is spent.
       Bucket-aware: an archive-triggered trip does not silence live.
    2. The bucket's own ceiling, where it has one (ARCHIVE does not).
    3. Live's forward-looking reserve, so backfill cannot eat the requests the
       price loop still needs before rollover. Enforced against every non-live
       bucket, within this plan, and now *unconditionally* -- it used to be
       skipped whenever the provider had not disclosed a limit, which is most
       days, and that is how archive came to spend a whole wallet before dawn.
    4. Archive's paced share of the day, so the leftover is spread across 24h
       instead of burned at midnight.
    """
    if is_plan_blocked(
        plan, bucket=bucket, admit=not holding_probe, holding_probe=holding_probe
    ):
        raise QuotaExhausted(
            f"Provider reported the {plan} plan exhausted; paused until reset.",
            reason=REASON_PLAN_BLOCKED,
        )
    # Simulate the live plan and the paced allowance *before* locking the
    # quota row. Both walk LiveFetchState / the price loop; holding
    # select_for_update across that stalls every other claimant, including live.
    preview = ApiRequestQuota.objects.filter(day=quota_day(), plan=plan).first()
    reserve = live_reserve_remaining(plan, preview) if bucket != LIVE else 0
    allowance = archive_allowance_now(plan, preview) if bucket == ARCHIVE else None
    field = f"{bucket}_used"
    with transaction.atomic():
        row = _quota_row(plan, locked=True)
        budget = bucket_budget(bucket, plan, row=row)
        if budget is not None and getattr(row, field) >= budget:
            raise QuotaExhausted(
                f"Daily {bucket} request budget exhausted ({budget}) on {plan}.",
                reason=REASON_BUCKET_EXHAUSTED,
            )
        if bucket != LIVE:
            ceiling = effective_limit(plan, row)
            if ceiling and row.used + reserve >= ceiling - _safety_margin():
                raise QuotaExhausted(
                    f"Remaining {plan} quota is reserved for live prices "
                    f"({reserve} req to cover the rest of the day).",
                    reason=REASON_LIVE_RESERVED,
                )
        if bucket == ARCHIVE:
            if allowance is not None and row.archive_used >= allowance:
                raise QuotaExhausted(
                    f"Archive is ahead of its paced share of the {plan} day "
                    f"({row.archive_used}/{allowance} permitted so far); waiting.",
                    reason=REASON_ARCHIVE_PACED,
                )

        _check_and_record_window(bucket)

        row.used += 1
        setattr(row, field, getattr(row, field) + 1)
        row.save(update_fields=["used", field, "updated_at"])
        return (row.limit - row.used) if row.limit else None


# Advisory-check cache. Process-local rather than Redis on purpose: this is a
# hint, staleness is harmless, and a network round trip would defeat the point.
_ROOM_TTL_SECONDS = 10.0
_room_cache = {}
_room_lock = threading.Lock()


def _archive_room_refusal(plan, now=None):
    """The reason an ARCHIVE reserve on `plan` would fail right now, or None.

    Mirrors `reserve_request`'s gates minus the row lock and the rate-limit
    window (which has a side effect and must only run for a request that is
    really about to happen).
    """
    if is_plan_blocked(plan, bucket=ARCHIVE):
        return (
            REASON_PLAN_BLOCKED,
            f"Provider reported the {plan} plan exhausted; paused until reset.",
        )
    row = ApiRequestQuota.objects.filter(day=quota_day(), plan=plan).first()
    if row is None:
        # No row yet means nothing has been spent today. Let the real gate create it.
        return None
    budget = bucket_budget(ARCHIVE, plan, row=row)
    if budget is not None and row.archive_used >= budget:
        return (
            REASON_BUCKET_EXHAUSTED,
            f"Daily {ARCHIVE} request budget exhausted ({budget}) on {plan}.",
        )
    reserve = live_reserve_remaining(plan, row, now=now)
    ceiling = effective_limit(plan, row)
    if ceiling and row.used + reserve >= ceiling - _safety_margin():
        return (
            REASON_LIVE_RESERVED,
            f"Remaining {plan} quota is reserved for live prices "
            f"({reserve} req to cover the rest of the day).",
        )
    allowance = archive_allowance_now(plan, row, now=now)
    if allowance is not None and row.archive_used >= allowance:
        return (
            REASON_ARCHIVE_PACED,
            f"Archive is ahead of its paced share of the {plan} day "
            f"({row.archive_used}/{allowance} permitted so far); waiting.",
        )
    return None


def require_archive_room(plan=TSETMC, *, now=None):
    """Raise `QuotaExhausted` if an ARCHIVE reserve on `plan` would clearly fail.

    Advisory, never authoritative: it takes no lock and claims nothing, so a
    caller that passes here must still go through `reserve_request`. Its only
    job is to let an expensive caller bail out *before* doing the work, rather
    than preparing a request the plan has no room for and throwing it away.

    On 2026-09-04 the tick branch of the archive worker spent 4,539 seconds of
    compute per wall-clock hour on 8,461 refused attempts, against 465 fetches
    that actually happened -- the pending-day computation runs first and the
    refusal came after it.

    The answer is cached per plan for ~10s. Both directions of staleness are
    safe: a stale "no room" costs one skipped cycle (the archive tick is 15s),
    and a stale "room" is corrected by the real gate a few lines later.
    """
    key = (plan, quota_day())
    with _room_lock:
        cached = _room_cache.get(key)
        if cached and time.monotonic() < cached[0]:
            refusal = cached[1]
        else:
            refusal = _archive_room_refusal(plan, now=now)
            _room_cache.clear()  # bounded: one live key, and the day rolls over
            _room_cache[key] = (time.monotonic() + _ROOM_TTL_SECONDS, refusal)
    if refusal:
        raise QuotaExhausted(refusal[1], reason=refusal[0])


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
    disclosed = reported_limit(account)
    usage = reported_usage(account)

    if usage is None and disclosed is None:
        return block

    with transaction.atomic():
        row = _quota_row(plan, locked=True)
        updates = []
        # Responses from concurrent requests can arrive out of order. Lowering
        # the local counter to an older response re-opens quota that was already
        # spent, while a larger counter is evidence of calls made outside this
        # process. Keep usage monotonic.
        #
        # The drift is deliberately NOT added to `other_used`. It did that until
        # 2026-09-09 to keep the three buckets summing to `used`, which is a tidy
        # invariant and a false one: the whole point of the drift is that we do
        # not know which lane spent it. Charging it to OTHER made a bucket with a
        # 200/day budget report 2,226 spent (2026-09-08), which zeroed the
        # reserve that keeps `catalog_sync` and `sync_symbol_metadata` alive and
        # made the console attribute the app's own blind spot to a named lane.
        # `unattributed_used` derives it instead, and nothing budgets against it.
        if usage is not None and usage > row.used:
            logger.info(
                "quota reconciled plan=%s day_usage %d->%d unattributed=%d",
                plan, row.used, usage, usage - row.used,
            )
            row.used = usage
            updates.append("used")
        if disclosed is not None and disclosed != row.limit:
            logger.info(
                "quota limit observed plan=%s %d->%d", plan, row.limit, disclosed
            )
            row.limit = disclosed
            updates.append("limit")
        if updates:
            row.save(update_fields=[*updates, "updated_at"])
    return block


def unattributed_used(row):
    """Requests the provider billed that no bucket of ours claims.

    Positive drift means something spent this plan without passing
    `reserve_request` -- a manual probe from a shell, another deployment holding
    the same key, or a code path that bypasses `fetchers.fetch_json`. It is the
    single number that says whether our accounting can be trusted, so it is
    derived and surfaced rather than hidden inside a bucket.
    """
    if row is None:
        return 0
    attributed = (row.archive_used or 0) + (row.live_used or 0) + (row.other_used or 0)
    return max(0, (row.used or 0) - attributed)


def remaining_requests(bucket=None, plan=TSETMC):
    """How many requests `bucket` may still spend on `plan` right now.

    Sizes archive batches and feeds the Ops console. It must agree with
    `reserve_request`, or the scheduler claims work the per-request gate then
    refuses -- which is how the ledger fills with `quota_exhausted` while nothing
    progresses. Same inputs, same order: effective ceiling, bucket budget, live
    reserve, then archive's paced share.
    """
    if is_plan_blocked(plan, bucket=bucket):
        return 0
    row = ApiRequestQuota.objects.filter(day=quota_day(), plan=plan).first()
    used = row.used if row else 0
    limit = effective_limit(plan, row)
    if not limit:
        # Unmetered plan: the provider is not counting, so there is no daily
        # remainder to report. Answer with one rolling window's worth -- the
        # only thing that actually shapes the rate -- because this number sizes
        # batches, and returning 0 froze the lane completely. That is what the
        # lapsed Market plan looked like from here: `archive_capacity()[brs]`
        # was 0, so `claim_archive_batch` skipped every gold and currency
        # endpoint outright while the provider was refusing nothing.
        return _window_limit(bucket or ARCHIVE)
    day_left = max(0, limit - _safety_margin() - used)
    if bucket is None:
        leftover = day_left
    else:
        leftover = day_left
        budget = bucket_budget(bucket, plan, row=row)
        if budget is not None:
            leftover = min(
                leftover, max(0, budget - (getattr(row, f"{bucket}_used") if row else 0))
            )
        if bucket != LIVE:
            leftover -= live_reserve_remaining(plan, row)
        if bucket == ARCHIVE:
            allowance = archive_allowance_now(plan, row)
            if allowance is not None:
                spent = getattr(row, "archive_used", 0) or 0
                leftover = min(leftover, max(0, allowance - spent))
        leftover = max(0, leftover)
    if _breaker_is_half_open(plan, bucket):
        # Live may advertise the one probe. Archive must not: the batcher
        # sizes off this number, and a leftover of 1 would look like room
        # and claim a full mixed batch that then parks until rollover.
        if bucket == ARCHIVE:
            return 0
        return min(1, leftover if bucket is not None else day_left)
    return leftover if bucket is not None else day_left


def archive_capacity():
    """Archive room across every plan, as `{plan: remaining}`.

    A single archive batch is a mixed bag -- `gold_daily` bills BRS while every
    stock endpoint bills TSETMC -- so the scheduler asks about all wallets at
    once. Summing to one number here is what would reintroduce the original bug.

    `claim_archive_batch` reads this per plan and skips the endpoints whose
    wallet is empty (`archive._ENDPOINT_PLAN`). It used to claim blind and let
    `reserve_request` refuse each call, which is why a spent TSETMC meter still
    produced 11,053 refusals an hour for as long as BRS had room. The per-request
    reserve remains the authority; it is no longer the first line of defence.

    Note this is the *paced* remainder, not the raw wallet, so a plan reads 0
    during a pacing wait and recovers on its own as the ramp opens.
    """
    return {plan: remaining_requests(ARCHIVE, plan) for plan in PLANS}


def archive_idle_reason(plan):
    """Why ARCHIVE currently cannot spend on `plan`, or None if it can.

    Distinguishes a paced wait (`archive_paced`) from a real empty wallet
    (`archive_budget_empty`) and from a tripped breaker (`plan_blocked`), so the
    scheduler ledger does not lump "waiting until later today" in with
    exhaustion.
    """
    if remaining_requests(ARCHIVE, plan) > 0:
        return None
    if is_plan_blocked(plan, bucket=ARCHIVE):
        return "plan_blocked"
    row = ApiRequestQuota.objects.filter(day=quota_day(), plan=plan).first()
    allowance = archive_allowance_now(plan, row)
    spent = getattr(row, "archive_used", 0) if row else 0
    if allowance is not None and spent >= allowance:
        return "archive_paced"
    return "archive_budget_empty"


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
            # What the provider itself last reported. 0 means **unmetered** --
            # a free product it does not count (`usage_today_limit: 0`) -- which
            # is a real answer, not a missing one. `metered` says which.
            "limit": row.limit if row else 0,
            "metered": bool(effective_limit(plan, row)),
            # Requests the provider billed that no lane of ours claims. This is
            # the number that says whether the rest of this block can be
            # trusted; a persistently rising value means something is spending
            # the key outside `reserve_request`.
            "unattributed": unattributed_used(row),
            # What the reserve maths actually used: disclosed if we have it,
            # otherwise the configured expectation. Shown so an operator can see
            # the difference between "provider said 10,000" and "we assumed it".
            "effective_limit": effective_limit(plan, row),
            "used": row.used if row else 0,
            "archive_used": row.archive_used if row else 0,
            "live_used": row.live_used if row else 0,
            "other_used": row.other_used if row else 0,
            "blocked": is_plan_blocked(plan),
            "live_blocked": is_plan_blocked(plan, bucket=LIVE),
            "remaining_archive": remaining_requests(ARCHIVE, plan),
            "remaining_live": remaining_requests(LIVE, plan),
            "live_reserve": live_reserve_remaining(plan, row),
            "archive_day_ceiling": archive_day_ceiling(plan, row),
            "archive_allowance_now": archive_allowance_now(plan, row),
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
        # Live's budget is per-plan now (BRS's wallet is smaller than the flat
        # configured figure), so the cross-plan number is their sum.
        "live_budget": sum(bucket_budget(LIVE, plan) for plan in PLANS),
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

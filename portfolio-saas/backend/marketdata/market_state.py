"""Is the Tehran Stock Exchange trading right now, and how fast should we poll?

Two signals, in priority order:

1. The local clock (free). TSE trades Saturday through Wednesday, roughly 08:30 to
   13:00 Tehran; Thursday and Friday are the Iranian weekend. Iran observes no DST
   (abolished 2022), so the offset is a flat +03:30.
2. The provider's `state` field (costs nothing extra -- it rides along on any
   `Index.php` response we already paid for). It is the only signal that knows
   about public holidays, which cannot be derived from a local calendar. It can
   only ever *close* a session the clock thinks is open, never open one the clock
   thinks is closed, so a stale cache can waste a poll but never miss the market.

# ponytail: no holiday calendar and no half-day handling. On an unobserved holiday
# the live loop polls a frozen order book for one day until an Index.php response
# refreshes the cached state. Fixing it properly means a holiday table.
"""
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import jdatetime
from django.conf import settings

from portfolio.live.redis_client import get_redis

logger = logging.getLogger(__name__)

OPEN = "open"
CLOSED_DAYTIME = "closed_daytime"
OVERNIGHT = "overnight"

TEHRAN = ZoneInfo("Asia/Tehran")

# jdatetime weekday(): 0 = Saturday ... 5 = Thursday, 6 = Friday.
TRADING_WEEKDAYS = frozenset({0, 1, 2, 3, 4})

SESSION_START = (8, 30)
SESSION_END = (13, 0)

# Daytime is when gold/currency desks are active even with the TSE shut.
DAYTIME_START = (7, 0)
DAYTIME_END = (23, 0)

# The provider spells a closed market this way. Quoted verbatim from the API
# response; it is Persian for "closed" and must be compared as a literal.
PROVIDER_CLOSED = "بسته"

_STATE_KEY = "marketdata:tse_state"
_STATE_TTL_OPEN = 1800
_PROBE_KEY = "marketdata:tse_state_probe"


def _now_tehran():
    return datetime.now(dt_timezone.utc).astimezone(TEHRAN)


def _within(now, start, end):
    return start <= (now.hour, now.minute) < end


def remember_provider_state(payload, *, now=None):
    """Cache the `state` string off any Index.php response we already paid for."""
    state = None
    if isinstance(payload, dict):
        state = payload.get("state")
    elif isinstance(payload, list):
        for record in payload:
            if isinstance(record, dict) and record.get("state"):
                state = record["state"]
                break
    if not state:
        return None
    now = now or _now_tehran()
    if str(state).strip() == PROVIDER_CLOSED:
        session_end = now.replace(
            hour=SESSION_END[0], minute=SESSION_END[1], second=0, microsecond=0
        )
        ttl = max(60, int((session_end - now).total_seconds()))
    else:
        ttl = _STATE_TTL_OPEN
    client = get_redis()
    if client is not None:
        try:
            client.set(_STATE_KEY, state, ex=ttl)
        except Exception as exc:
            logger.warning("could not cache market state: %s", exc)
    return state


def claim_provider_state_probe(now=None):
    """Claim the single half-hourly Index.php probe for the live workers."""
    now = now or _now_tehran()
    # The clock must get one chance to reopen a stale provider-closed cache at
    # the next session. Otherwise a pre-open "closed" response can suppress
    # every TSE fetch until the cached TTL expires after the session has ended.
    if market_state_at(now) != OPEN:
        return False
    client = get_redis()
    if client is None:
        return True
    try:
        return bool(client.set(_PROBE_KEY, "1", ex=_STATE_TTL_OPEN, nx=True))
    except Exception as exc:
        logger.warning("could not claim market-state probe: %s", exc)
        return True


def release_provider_state_probe():
    """Allow the next live cycle to retry a failed provider-state probe."""
    client = get_redis()
    if client is not None:
        try:
            client.delete(_PROBE_KEY)
        except Exception as exc:
            logger.warning("could not release market-state probe: %s", exc)


def _provider_says_closed():
    client = get_redis()
    if client is None:
        return False
    try:
        cached = client.get(_STATE_KEY)
    except Exception:
        return False
    if cached is None:
        return False
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8", "replace")
    return cached.strip() == PROVIDER_CLOSED


def market_state_at(now, *, provider_closed=False):
    """Return the market state for one Tehran-local timestamp."""
    jalali_weekday = jdatetime.date.fromgregorian(date=now.date()).weekday()
    trading_day = jalali_weekday in TRADING_WEEKDAYS
    if trading_day and _within(now, SESSION_START, SESSION_END) and not provider_closed:
        return OPEN
    if _within(now, DAYTIME_START, DAYTIME_END):
        return CLOSED_DAYTIME
    return OVERNIGHT


def market_state():
    """Return OPEN, CLOSED_DAYTIME, or OVERNIGHT right now."""
    return market_state_at(_now_tehran(), provider_closed=_provider_says_closed())


def live_job_keys(
    *, state, now, has_brs, has_tsetmc, ignore_hours=False,
    include_state_probe=False,
):
    """The exact provider jobs one live cycle is allowed to execute.

    The gold/currency job runs in EVERY state, including OVERNIGHT. It used to be
    gated to OPEN/CLOSED_DAYTIME, which meant 23:00-07:00 Tehran fetched no price
    of any kind: measured on 2026-08-30 through 09-03, hours 00-06 are empty in
    the price table. That gate was written when the job meant "the domestic
    gold/coin desks", which really are shut overnight -- but the same payload now
    carries crypto and hard currency, which trade around the clock, and the
    unmetered origins behind it (TGJU, Wallex, Nobitex) cost nothing to poll at
    3am. Eight hours of frozen crypto prices is a much worse trade than the ~160
    overnight requests this costs against a meter with room to spare.

    The TSE job stays session-gated. That one is not a cadence choice: outside
    08:30-13:00 the order book genuinely does not move, so polling it buys a
    byte-identical payload off the binding 10,000/day meter.
    """
    jobs = []
    if has_brs:
        jobs.append("gold_currency")
    if has_tsetmc and (ignore_hours or state == OPEN):
        if include_state_probe:
            jobs.append("market_index")
        jobs.append("tsetmc")
    return tuple(jobs)


def expects_live_prices():
    """Whether some live job should be running right now.

    Now true around the clock: since the gold/currency job lost its overnight
    gate (see `live_job_keys`), there is no hour with zero scheduled jobs, so a
    stale Price row at 03:00 IS a fault and the watchdog should say so.

    This deliberately arms `config.health.PriceFeedView` -- the dead-man's switch
    the on-VPS watchdog and the GitHub Actions probe both poll -- for eight hours
    it previously ignored. That is the point: those were the hours in which a
    dead price loop was indistinguishable from a working one.
    """
    return True


# Daily series only gain a new row after the session closes, so a state that
# verifies at midday and defers a flat 20h wakes up before the next close and
# verifies the same stale history again. Half the tracked universe sat one
# trading day behind for exactly this reason. Aim the refresh at the first
# moment the new candle can exist.
POST_CLOSE_REFRESH = (14, 30)


def next_post_close(now=None):
    """UTC datetime of the next post-close refresh window, always in the future."""
    now = now or datetime.now(dt_timezone.utc)
    local = now.astimezone(TEHRAN)
    target = local.replace(
        hour=POST_CLOSE_REFRESH[0], minute=POST_CLOSE_REFRESH[1],
        second=0, microsecond=0,
    )
    for _ in range(8):
        weekday = jdatetime.date.fromgregorian(date=target.date()).weekday()
        if target > local and weekday in TRADING_WEEKDAYS:
            return target.astimezone(dt_timezone.utc)
        target += timedelta(days=1)
    return (local + timedelta(hours=20)).astimezone(dt_timezone.utc)


def live_interval_seconds():
    """Seconds the live loop should wait before the next fetch.

    Budget at the current 60/90/180 cadences, per trading day: ~270 polls while
    the TSE is open, ~460 daytime and ~160 overnight. The stock lane bills only
    the open ones against the 10,000/day TSETMC meter. The Market product is
    currently unmetered, and free origins cover most gold/FX cycles. Full
    arithmetic lives in `config.settings`.
    """
    return {
        OPEN: settings.MARKETDATA_LIVE_INTERVAL_OPEN,
        CLOSED_DAYTIME: settings.MARKETDATA_LIVE_INTERVAL_DAYTIME,
        OVERNIGHT: settings.MARKETDATA_LIVE_INTERVAL_OVERNIGHT,
    }[market_state()]

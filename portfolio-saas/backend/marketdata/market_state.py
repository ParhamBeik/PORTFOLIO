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

import jdatetime
from django.conf import settings

from portfolio.live.pubsub import get_redis

logger = logging.getLogger(__name__)

OPEN = "open"
CLOSED_DAYTIME = "closed_daytime"
OVERNIGHT = "overnight"

# Tehran local time, flat offset (no DST in Iran since 2022).
TEHRAN = dt_timezone(timedelta(hours=3, minutes=30))

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
_STATE_TTL = 3600


def _now_tehran():
    return datetime.now(dt_timezone.utc).astimezone(TEHRAN)


def _within(now, start, end):
    return start <= (now.hour, now.minute) < end


def remember_provider_state(payload):
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
    client = get_redis()
    if client is not None:
        try:
            client.set(_STATE_KEY, state, ex=_STATE_TTL)
        except Exception:
            logger.warning("Could not cache market state", exc_info=True)
    return state


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


def market_state():
    """Return OPEN, CLOSED_DAYTIME, or OVERNIGHT."""
    now = _now_tehran()
    jalali_weekday = jdatetime.date.fromgregorian(date=now.date()).weekday()
    trading_day = jalali_weekday in TRADING_WEEKDAYS
    if trading_day and _within(now, SESSION_START, SESSION_END) and not _provider_says_closed():
        return OPEN
    if _within(now, DAYTIME_START, DAYTIME_END):
        return CLOSED_DAYTIME
    return OVERNIGHT


def is_market_open():
    return market_state() == OPEN


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

    Budget: ~270 polls while the TSE is open (2 calls each), ~200 daytime and ~36
    overnight at 1 call each -- roughly 780 requests/day against a 800-request live
    allowance, versus 1,440 for the old flat 2-minute loop.
    """
    return {
        OPEN: settings.MARKETDATA_LIVE_INTERVAL_OPEN,
        CLOSED_DAYTIME: settings.MARKETDATA_LIVE_INTERVAL_DAYTIME,
        OVERNIGHT: settings.MARKETDATA_LIVE_INTERVAL_OVERNIGHT,
    }[market_state()]

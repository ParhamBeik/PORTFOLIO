"""Cadence-driven live endpoints: seeding, claiming, and costing the day's plan.

`archive.py` is the equivalent for backfill. The split is deliberate: an archive
state converges on a finite target and stops, while a live state repeats forever
on a cadence, so "how much is left to do" means completely different things and
one table could not answer both.

The reason this module exists at all is quota arithmetic. `quota.live_reserve_remaining`
has to know what live will spend between now and the Tehran day rollover before it
can tell the archive what it may have. It used to simulate only the 2-minute price
loop, so the 5-minute snapshot beats (crypto, commodity, ETF NAV, TSE options, IME
futures/options) spent from the live bucket entirely unaccounted for. Pricing the
plan from the same rows the scheduler claims from is what keeps the two honest.
"""
import logging
import math
from datetime import datetime, timedelta

import jdatetime
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import market_state
from .models import LiveFetchState, MarketInstrument

logger = logging.getLogger(__name__)

# Seed cadences. Deliberately coarser than the old flat 300s beat: every one of
# these feeds a daily OHLC bar (`aggregate_market_daily_bars`), not the held-asset
# price loop, so a 5-minute poll bought ~1,440 requests/day of resolution that
# nothing downstream reads. Rows are DB-editable afterwards; these are seeds only.
_MARKET_WIDE_SEEDS = {
    "crypto": (900, False),
    "commodity": (900, False),
    "option_contracts": (900, True),
    "ime_futures": (900, True),
    "ime_options": (900, True),
}

# Nav.php is one fund per request and there are ~417 of them. Once per trading day
# guarantees every fund a fresh NAV for its daily bar at ~417 requests/day; the
# rotating-cursor batch it replaces guaranteed nothing.
_ETF_NAV_CADENCE = 86_400


def ensure_live_states():
    """Create missing rows and retire catalog-managed stale ETF rows.

    Cadence is an operator-tunable column, so re-seeding must not silently revert
    a hand-tuned value the way a blanket update_or_create would.
    """
    eligible_etfs = set(
        MarketInstrument.objects.filter(
            category=MarketInstrument.Category.ETF, eligible=True
        ).values_list("symbol", flat=True)
    )
    etf_states = LiveFetchState.objects.filter(endpoint_key="etf_nav")
    etf_states.filter(enabled=True).exclude(scope__in=eligible_etfs).update(
        enabled=False, last_error="stale_catalog"
    )
    etf_states.filter(
        enabled=False, last_error="stale_catalog", scope__in=eligible_etfs
    ).update(enabled=True, last_error="")

    rows = [
        LiveFetchState(
            endpoint_key=key,
            scope="",
            cadence_seconds=cadence,
            session_only=session_only,
        )
        for key, (cadence, session_only) in _MARKET_WIDE_SEEDS.items()
    ]
    rows += [
        LiveFetchState(
            endpoint_key="etf_nav",
            scope=symbol,
            cadence_seconds=_ETF_NAV_CADENCE,
            session_only=True,
        )
        for symbol in sorted(eligible_etfs)
    ]
    existing = set(LiveFetchState.objects.values_list("endpoint_key", "scope"))
    missing = [r for r in rows if (r.endpoint_key, r.scope) not in existing]
    if missing:
        LiveFetchState.objects.bulk_create(missing, ignore_conflicts=True)
    return len(missing)


def _firings_until(state, start, end):
    """How many times one state fires in [start, end), honouring session gating.

    Counted on the calendar rather than stepped one cadence at a time: ETF NAV
    alone is ~417 rows, and a per-row simulation loop at 900s granularity would
    run millions of iterations every time the reserve is read -- and the reserve
    is read on every single archive request.
    """
    if not state.enabled or end <= start:
        return 0
    cadence = max(1, state.cadence_seconds)
    cursor = state.next_attempt_at or start
    if cursor < start:
        cursor = start
    if not state.session_only:
        if cursor >= end:
            return 0
        return math.ceil((end - cursor).total_seconds() / cadence)

    # Session-gated: only the overlap with each day's trading window counts.
    total = 0
    day = cursor.astimezone(market_state.TEHRAN).date()
    last_day = end.astimezone(market_state.TEHRAN).date()
    while day <= last_day:
        open_at, close_at = _session_bounds(day)
        if open_at is not None:
            window_start = max(open_at, cursor, start)
            window_end = min(close_at, end)
            if window_end > window_start:
                total += math.ceil((window_end - window_start).total_seconds() / cadence)
        day += timedelta(days=1)
    return total


def _session_bounds(day):
    """Session open/close for one Tehran calendar day, or (None, None) if shut."""
    weekday = jdatetime.date.fromgregorian(date=day).weekday()
    if weekday not in market_state.TRADING_WEEKDAYS:
        return None, None
    open_at = datetime(
        day.year, day.month, day.day,
        *market_state.SESSION_START, tzinfo=market_state.TEHRAN,
    )
    close_at = datetime(
        day.year, day.month, day.day,
        *market_state.SESSION_END, tzinfo=market_state.TEHRAN,
    )
    return open_at, close_at


def planned_requests(start, end):
    """Total live-bucket requests these states will spend across [start, end)."""
    return sum(
        _firings_until(state, start, end)
        for state in LiveFetchState.objects.filter(enabled=True)
    )


def day_start():
    """Midnight Tehran for the current quota day, as an aware datetime."""
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE)
    return datetime.combine(
        timezone.now().astimezone(zone).date(), dtime.min, tzinfo=zone
    )


def full_day_cost(states=None):
    """What these states cost over one complete 24h window.

    Independent of the time of day on purpose: a mid-afternoon reading of the
    remaining plan would size a budget at half the real need. `states=None` costs
    the whole enabled table.
    """
    if states is None:
        states = LiveFetchState.objects.filter(enabled=True)
    start = day_start()
    total = 0
    for state in states:
        # Cadence is measured from `next_attempt_at`, which is meaningless for a
        # hypothetical fresh day.
        state.next_attempt_at = None
        total += _firings_until(state, start, start + timedelta(days=1))
    return total


def claim_due(endpoint_key, limit=None):
    """Lease the due states for one endpoint, newest-deadline-last.

    Leases by pushing `next_attempt_at` forward before the caller fetches, the
    same shape `archive.claim_archive_batch` uses, so a second worker on the same
    beat cannot double-spend the quota for one scheduled firing.
    """
    now = timezone.now()
    with transaction.atomic():
        query = (
            LiveFetchState.objects.select_for_update(skip_locked=True)
            .filter(enabled=True, endpoint_key=endpoint_key)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .order_by("next_attempt_at", "scope")
        )
        states = list(query[:limit] if limit else query)
        if not states:
            return []
        session_open = (
            getattr(settings, "MARKETDATA_IGNORE_MARKET_HOURS", False)
            or market_state.market_state() == market_state.OPEN
        )
        claimed = [s for s in states if session_open or not s.session_only]
        if not claimed:
            return []
        for state in claimed:
            state.next_attempt_at = now + timedelta(seconds=max(1, state.cadence_seconds))
            state.last_attempt_at = now
        LiveFetchState.objects.bulk_update(
            claimed, ["next_attempt_at", "last_attempt_at"]
        )
    return claimed


def record_result(state, *, ok, error=""):
    """Mark one claimed state's outcome. A failure does not re-arm early."""
    now = timezone.now()
    if ok:
        state.last_success_at = now
        state.consecutive_failures = 0
        state.last_error = ""
    else:
        state.consecutive_failures += 1
        state.last_error = str(error)[:500]
    state.save(update_fields=["last_success_at", "consecutive_failures", "last_error"])

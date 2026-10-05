"""Crawl codal.ir search one Jalali day at a time (docs/CODAL-DIRECT-MIGRATION.md §5).

Each `step()` spends exactly one search request. Priority: the live window
(today and the last few days, re-crawled because letters keep arriving), then
completed days due a re-check, then the newest day not yet complete, walking
back toward `CODAL_DISCOVERY_OLDEST_DAY`.

Phase 1 is shadow mode: only CodalLetter and CodalDiscoveryDay are written.
"""
from datetime import timedelta

import jdatetime
from django.conf import settings
from django.db.models import Min, Q
from django.utils import timezone

from . import jalali
from .models import CodalDiscoveryDay, CodalLetter
from .sources import codal_search

# Re-crawl a complete live day once it is this old: today moves fast, the few
# days behind it pick up late publications and corrections.
_LIVE_STALE = {0: timedelta(minutes=15)}
_LIVE_STALE_DEFAULT = timedelta(hours=6)

_UPDATE_FIELDS = [
    name for name in (f.name for f in CodalLetter._meta.concrete_fields)
    if name not in ("id", "tracing_no", "first_seen_at")
]


def tehran_today():
    from .market_state import _now_tehran

    return jalali.from_gregorian(_now_tehran())


def shift(day, days):
    year, month, dom = (int(part) for part in day.split("-"))
    return (jdatetime.date(year, month, dom) - jdatetime.timedelta(days=days)).strftime("%Y-%m-%d")


def _reopen(state):
    state.verified_complete = False
    state.next_page = 1
    return state


def pick_day(now=None):
    """The day the next request should go to, or None when there is nothing due."""
    now = now or timezone.now()
    today = tehran_today()
    live = [shift(today, offset) for offset in range(settings.CODAL_DISCOVERY_LIVE_DAYS + 1)]

    for offset, day in enumerate(live):
        state, _ = CodalDiscoveryDay.objects.get_or_create(date=day)
        if state.next_check_at and state.next_check_at > now and not state.verified_complete:
            continue  # backing off after an error
        if not state.verified_complete:
            return state
        stale = _LIVE_STALE.get(offset, _LIVE_STALE_DEFAULT)
        if not state.last_success_at or now - state.last_success_at >= stale:
            return _reopen(state)

    due = (
        CodalDiscoveryDay.objects.filter(verified_complete=True, next_check_at__lte=now)
        .exclude(date__in=live).order_by("-date").first()
    )
    if due:
        return _reopen(due)

    pending = (
        CodalDiscoveryDay.objects.filter(verified_complete=False)
        .filter(Q(next_check_at__isnull=True) | Q(next_check_at__lte=now))
        .exclude(date__in=live).order_by("-date").first()
    )
    if pending:
        return pending

    oldest = CodalDiscoveryDay.objects.aggregate(oldest=Min("date"))["oldest"] or live[-1]
    if oldest <= settings.CODAL_DISCOVERY_OLDEST_DAY:
        return None
    state, _ = CodalDiscoveryDay.objects.get_or_create(date=shift(oldest, 1))
    return state


def _recheck_after(day, today):
    age = (jalali.to_gregorian(today) - jalali.to_gregorian(day)).days
    return timedelta(days=7) if age <= 30 else timedelta(days=180)


def step(state, now=None):
    """Fetch one page for `state`, store its letters, advance or complete the day.

    Completion is decided by counting stored letters for the day against the
    origin's `Total`, not by reaching the last page: new letters land at the top
    of a newest-first list and shift every page under a crawl in progress.
    """
    now = now or timezone.now()
    state.last_attempt_at = now
    try:
        result = codal_search.fetch_letters(state.date, state.next_page)
    except codal_search.CodalSearchThrottled:
        state.save()
        raise
    except Exception as exc:
        state.consecutive_failures += 1
        state.last_error = f"{type(exc).__name__}: {exc}"[:500]
        state.next_check_at = now + min(
            timedelta(minutes=10) * 2 ** state.consecutive_failures, timedelta(hours=24)
        )
        state.save()
        raise

    letters = result["letters"]
    if letters:
        CodalLetter.objects.bulk_create(
            [CodalLetter(**row) for row in letters],
            update_conflicts=True,
            unique_fields=["tracing_no"],
            update_fields=_UPDATE_FIELDS,
        )
    state.total = result["total"]
    stored = CodalLetter.objects.filter(date_publish=state.date).count()
    if stored >= state.total:
        state.consecutive_failures = 0
        state.last_error = ""
        state.verified_complete = True
        state.checks += 1
        state.next_page = 1
        state.last_success_at = now
        state.next_check_at = now + _recheck_after(state.date, tehran_today())
    elif state.next_page >= result["pages"]:
        # Reached the end short: something shifted under the crawl, or the origin
        # counts a letter it never lists. Go round again, backing off so one
        # stubborn day cannot hold the backfill behind it.
        state.next_page = 1
        state.consecutive_failures += 1
        state.last_error = f"short: stored {stored} of {state.total}"
        state.next_check_at = now + min(
            timedelta(minutes=10) * 2 ** state.consecutive_failures, timedelta(hours=24)
        )
    else:
        state.next_page += 1
        state.next_check_at = None
        if not state.last_error.startswith("short:"):
            state.consecutive_failures = 0
            state.last_error = ""
    state.save()
    return {"date": state.date, "page_letters": len(letters), "stored": stored,
            "total": state.total, "complete": state.verified_complete}

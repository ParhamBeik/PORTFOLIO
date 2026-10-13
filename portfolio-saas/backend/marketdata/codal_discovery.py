"""Crawl codal.ir search one Jalali day at a time (docs/CODAL-DIRECT-MIGRATION.md §5).

Each `step()` spends exactly one search request. Priority: the live window
(today and the last few days, re-crawled because letters keep arriving), then
completed days due a re-check, then the newest day not yet complete, walking
back toward `CODAL_DISCOVERY_OLDEST_DAY`.

Every letter lands in CodalLetter (market-wide, a faithful copy). With
`CODAL_DISCOVERY_SOURCE=codal` (phase 2) letters for catalog symbols are also
promoted into CodalAnnouncement, which is what the extraction pipeline reads;
BrsApi's Codal list is then switched off (`archive.disabled_endpoints`).
"""
import re
from datetime import timedelta

import jdatetime
from django.conf import settings
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from . import jalali
from .models import CodalAnnouncement, CodalDiscoveryDay, CodalLetter, CodalReport
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
    try:
        if letters:
            CodalLetter.objects.bulk_create(
                [CodalLetter(**row) for row in letters],
                update_conflicts=True,
                unique_fields=["tracing_no"],
                update_fields=_UPDATE_FIELDS,
            )
            if promoting():
                promote(letters)
    except Exception as exc:
        # A write failure is the day's failure too. Outside this guard it skipped
        # the backoff below, so one over-long symbol re-fetched the same page
        # every minute (41 requests a day of a ~550/day budget).
        state.consecutive_failures += 1
        state.last_error = f"{type(exc).__name__}: {exc}"[:500]
        state.next_check_at = now + min(
            timedelta(minutes=10) * 2 ** state.consecutive_failures, timedelta(hours=24)
        )
        state.save()
        raise
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


def promoting():
    """Phase 2: codal.ir, not BrsApi, writes the filing list the pipeline reads."""
    return settings.CODAL_DISCOVERY_ENABLED and settings.CODAL_DISCOVERY_SOURCE == "codal"


def withheld_documents():
    """Filings codal.ir wrote whose documents are not fetched (yet).

    Older than the live window and outside `CODAL_HISTORY_DOCUMENTS`. Without
    this, promoting the crawled history queued every document it ever listed
    -- the bulk download the storage policy rules out.
    """
    scope = settings.CODAL_HISTORY_DOCUMENTS
    if scope == "all":
        return Q(pk__in=[])
    held = Q(source=CodalAnnouncement.Source.CODAL,
             date_publish__lt=shift(tehran_today(), settings.CODAL_DISCOVERY_LIVE_DAYS))
    if scope == "annual_statements":
        held &= ~(Q(code="ن-10") & Q(title__contains="سال مالی"))
    return held


def discovery_status():
    """What the codal.ir crawl has covered, market-wide (one row per Jalali day).

    Once promoting, this -- not `ArchiveFetchState(codal_announcements)`, which
    `disabled_endpoints()` freezes -- is the filing list's coverage.
    """
    days = CodalDiscoveryDay.objects.aggregate(
        days=Count("id"),
        complete=Count("id", filter=Q(verified_complete=True)),
        failing=Count("id", filter=Q(verified_complete=False, consecutive_failures__gt=0)),
        oldest=Min("date"), newest=Max("date"),
        last_success_at=Max("last_success_at"),
    )
    days["last_success_at"] = days["last_success_at"].isoformat() if days["last_success_at"] else None
    # Days are created newest-first as the crawl walks back; all of them
    # reconciled says nothing until the walk has reached its configured floor.
    days["reached_oldest"] = bool(days["oldest"]) and days["oldest"] <= settings.CODAL_DISCOVERY_OLDEST_DAY
    return {
        "enabled": bool(settings.CODAL_DISCOVERY_ENABLED),
        "promoting": promoting(),
        **days,
        "letters": CodalLetter.objects.count(),
        "promoted": CodalAnnouncement.objects.filter(source=CodalAnnouncement.Source.CODAL).count(),
    }


def _absolute(url):
    """codal.ir answers "/Reports/..." and "DownloadFile.aspx?..."; BrsApi gave full URLs."""
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return "https://codal.ir" + (url if url.startswith("/") else "/" + url)


_IDENTITY_FIELDS = ("tracing_no", "letter_serial", "letter_type", "has_excel", "has_pdf", "has_attachment")
_LINK_FIELDS = ("link", "link_pdf", "link_excel", "link_attachment")


def _letter_fields(row):
    """The fields codal.ir owns on a row it wrote, links absolute.

    Only an explicit `Has*=False` means a document is absent; a null flag keeps
    the URL.
    """
    from .codal_classification import classify

    classification = classify(row["title"])
    return {
        "company_name": row["company_name"],
        "title": row["title"],
        "date_send": row["date_sent"],
        "time_send": row["time_sent"],
        "link": _absolute(row["url"]),
        "link_pdf": _absolute(row["pdf_url"]) if row["has_pdf"] is not False else "",
        "link_excel": _absolute(row["excel_url"]) if row["has_excel"] is not False else "",
        "link_attachment": _absolute(row["attachment_url"]) if row["has_attachment"] is not False else "",
        "doc_type": classification.doc_type,
        "tier": classification.tier,
        "classified_by": classification.classified_by,
    }


def promote(letters):
    """Write crawled letters for catalog symbols into CodalAnnouncement.

    Matches the row BrsApi already wrote by `letter_serial`, then by the old
    unique key, and only fills the identity codal.ir adds -- title, symbol and
    links that differ are the provider's record and stay. Everything else is
    inserted with absolute links, and only for the documents codal.ir says
    exist: `HasExcel=false` was the ~12k `HTTP500@excel.codal.ir` class.
    New rows go straight to extraction, as BrsApi-era rows always did.
    """
    from .ingest import _enqueue_codal_extractions

    catalog = _promotable_symbols()
    created, requeue = [], []
    for row in letters:
        if row["symbol"] not in catalog:
            continue
        identity = {field: row[field] for field in _IDENTITY_FIELDS}
        existing = None
        if row["letter_serial"]:
            existing = CodalAnnouncement.objects.filter(letter_serial=row["letter_serial"]).first()
        if existing is None:
            existing = CodalAnnouncement.objects.filter(
                symbol=row["symbol"], code=row["letter_code"],
                date_publish=row["date_publish"], time_publish=row["time_publish"],
            ).first()
        if existing is not None:
            if existing.tracing_no not in (None, row["tracing_no"]):
                continue  # already claimed by a different letter: leave both alone
            if CodalAnnouncement.objects.filter(tracing_no=row["tracing_no"]).exclude(pk=existing.pk).exists():
                continue
            if existing.source != CodalAnnouncement.Source.CODAL:
                CodalAnnouncement.objects.filter(pk=existing.pk).update(**identity)
                continue
            # Our own row: codal.ir is its record, so a re-crawl that changed the
            # title or made a document available is applied, and a newly listed
            # document is fetched instead of staying an empty link forever.
            fields = {**identity, **_letter_fields(row)}
            gained = any(fields[link] and not getattr(existing, link) for link in _LINK_FIELDS)
            CodalAnnouncement.objects.filter(pk=existing.pk).update(**fields)
            if gained:
                requeue.append(existing.pk)
            continue
        if CodalAnnouncement.objects.filter(tracing_no=row["tracing_no"]).exists():
            continue
        announcement, made = CodalAnnouncement.objects.get_or_create(
            symbol=row["symbol"], code=row["letter_code"],
            date_publish=row["date_publish"], time_publish=row["time_publish"],
            defaults={**identity, "source": CodalAnnouncement.Source.CODAL, **_letter_fields(row)},
        )
        if made:
            created.append(announcement)
    if created:
        _enqueue_codal_extractions(created)
    if requeue:
        # Already extracted: the retry sweep (`queue_codal_extractions`) re-runs
        # FAILED reports, and stored artifacts are read back, not re-fetched.
        CodalReport.objects.filter(announcement_id__in=requeue).update(
            status=CodalReport.Status.FAILED, error_code="new_document_listed",
        )
    return len(created)


def _promotable_symbols():
    """The universe BrsApi's Codal archive covered: tracked TSE symbols, no board copies.

    Documents are stored forever, so this is a disk decision. A bare catalog
    lookup also admitted ineligible TSETMC rows and BRS symbols that happen to
    share a name; the full market is projected past the shared disk.
    """
    from .tasks import tracked_tse_symbols

    return {symbol for symbol in tracked_tse_symbols() if not re.search(r"\d$", symbol)}


def promote_backlog(limit=200):
    """Promote letters crawled before promotion was on (shadow mode), a slice at a time.

    Cheap and idempotent: a letter whose `tracing_no` is already on an
    announcement is never selected again, so this converges and then costs
    one indexed query per tick.
    """
    if not promoting():
        return 0
    pending = (
        CodalLetter.objects.filter(symbol__in=list(_promotable_symbols()))
        .exclude(tracing_no__in=CodalAnnouncement.objects.filter(tracing_no__isnull=False).values("tracing_no"))
        .order_by("-date_publish", "-time_publish")
    )
    fields = [f for f in _UPDATE_FIELDS if f not in ("raw", "last_seen_at")] + ["tracing_no"]
    letters = list(pending.values(*fields)[:limit])
    return promote(letters) if letters else 0

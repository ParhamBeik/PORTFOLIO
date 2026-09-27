"""Quota-bounded, date-windowed discovery of older Codal announcements.

The normal archive refresh verifies only its newest five pages. These windows
are a separate historical census: complete means every announcement returned
by this symbol/date query was read back from PostgreSQL, not that financial
statement extraction or issuer lineage is complete.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import ingest, jalali
from .archive import CODAL_PAGE_SIZE, _codal_keys, _codal_total
from .fetchers import MarketDataFetchError, fetch_codal_announcements
from .models import CodalAnnouncement, CodalHistoryWindow


class RequestBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    @property
    def remaining(self):
        return self.limit - self.used

    def fetch(self, symbol, date_start, date_end, page):
        if self.remaining <= 0:
            raise ValueError("Codal history request budget exhausted.")
        # Count conservatively even if quota admission rejects the call. The
        # shared fetcher makes the actual provider reservation before HTTP.
        self.used += 1
        return fetch_codal_announcements(
            settings.TSETMC_API_KEY,
            symbol=symbol,
            date_start=date_start,
            date_end=date_end,
            page=page,
        )


def _page_keys(payload, date_start, date_end):
    records = payload.get("announcement") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise MarketDataFetchError("Codal history page omitted announcements.")
    keys = _codal_keys(payload)
    if len(keys) != len(records) or any(
        not all(key) or jalali.to_gregorian(key[2]) is None
        or not date_start <= key[2] <= date_end
        for key in keys
    ):
        raise MarketDataFetchError("Codal history page has duplicate, invalid, or out-of-window keys.")
    return keys


def _stored_keys(expected, date_start, date_end):
    symbols = {key[0] for key in expected}
    if not symbols:
        return set()
    return {
        (symbol, code, date_publish, time_publish)
        for symbol, code, date_publish, time_publish in
        CodalAnnouncement.objects.filter(
            symbol__in=symbols,
            date_publish__gte=date_start,
            date_publish__lte=date_end,
        ).values_list("symbol", "code", "date_publish", "time_publish")
    } & expected


def _split(window):
    start = jalali.to_gregorian(window.date_start)
    end = jalali.to_gregorian(window.date_end)
    if start >= end:
        raise MarketDataFetchError("A single-day Codal window exceeds the page cap.")
    middle = start + timedelta(days=(end - start).days // 2)
    left_end = jalali.from_gregorian(middle)
    right_start = jalali.from_gregorian(middle + timedelta(days=1))
    with transaction.atomic():
        CodalHistoryWindow.objects.bulk_create(
            [
                CodalHistoryWindow(
                    symbol=window.symbol, date_start=window.date_start, date_end=left_end
                ),
                CodalHistoryWindow(
                    symbol=window.symbol, date_start=right_start, date_end=window.date_end
                ),
            ],
            ignore_conflicts=True,
        )
        window.split = True
        window.last_error = ""
        window.save(update_fields=["split", "last_error"])


def scan_window(window, budget, *, max_pages=10):
    """Scan one window or split it; never claim completeness for a partial scan."""
    if max_pages < 1 or budget.remaining < max_pages:
        raise ValueError("Reserve a full max-pages budget before scanning a window.")
    window.last_attempt_at = timezone.now()
    window.verified_complete = False
    window.save(update_fields=["last_attempt_at", "verified_complete"])

    first = budget.fetch(window.symbol, window.date_start, window.date_end, 1)
    total = _codal_total(first)
    first_keys = _page_keys(first, window.date_start, window.date_end)
    if total and len(first_keys) != min(total, CODAL_PAGE_SIZE):
        raise MarketDataFetchError("Codal history first page has an unexpected row count.")
    window.expected_rows = total
    window.save(update_fields=["expected_rows"])
    if total > max_pages * CODAL_PAGE_SIZE:
        _split(window)
        return "split"

    expected = set()
    pages = max(1, -(-total // CODAL_PAGE_SIZE))
    for page in range(1, pages + 1):
        payload = first if page == 1 else budget.fetch(
            window.symbol, window.date_start, window.date_end, page
        )
        if _codal_total(payload) != total:
            raise MarketDataFetchError("Codal history page count changed during scan.")
        keys = _page_keys(payload, window.date_start, window.date_end)
        ingest.ingest_codal(payload)
        expected |= keys
    if len(expected) != total:
        raise MarketDataFetchError(
            f"Codal history pages contained {len(expected)} distinct rows; expected {total}."
        )
    stored = _stored_keys(expected, window.date_start, window.date_end)
    window.stored_rows = len(stored)
    if stored != expected:
        window.save(update_fields=["stored_rows"])
        raise MarketDataFetchError(
            f"Codal history has {len(expected) - len(stored)} announcement(s) missing in storage."
        )
    window.verified_complete = True
    window.consecutive_failures = 0
    window.last_error = ""
    window.last_success_at = timezone.now()
    window.next_attempt_at = None
    window.save(update_fields=[
        "stored_rows", "verified_complete", "consecutive_failures", "last_error",
        "last_success_at", "next_attempt_at",
    ])
    return "complete"


def record_window_failure(window, error):
    failures = window.consecutive_failures + 1
    window.consecutive_failures = failures
    window.last_error = f"{type(error).__name__}: {error}"[:500]
    window.next_attempt_at = timezone.now() + timedelta(hours=min(2 ** (failures - 1), 24))
    window.verified_complete = False
    window.save(update_fields=[
        "consecutive_failures", "last_error", "next_attempt_at", "verified_complete",
    ])

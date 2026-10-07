"""codal.ir's own filing search: free discovery to replace BrsApi Codal/Announcement.php.

Measured 2026-10-05 from the production VPS (docs/CODAL-DIRECT-MIGRATION.md §3):

    GET search.codal.ir/api/search/v2/q?...&FromDate=1404/07/02&ToDate=1404/07/02&PageNumber=N
      -> {Total, Page (= page COUNT), Letters[<=20], IsAttacker}

A day crawled page by page returned exactly `Total` distinct TracingNo, so a
day can be proven complete. `FromDate=-1` is a 400 -- omit the parameter.

**The rate limit is the whole design problem.** The origin answers 429 with no
Retry-After after roughly 30 requests in a rolling hour, and knocking while
blocked appeared to extend the block (10 to 45 minutes). So this module never
retries inline: it keeps its own rolling-hour budget, starts low, halves it
and parks the origin on any 429 or `IsAttacker`, and earns it back slowly.
"""
import logging
import re
import time
from urllib.parse import unquote

from django.conf import settings
from django.core.cache import cache

from ..jalali import fold_digits, normalize_jalali
from .http import SourceResponseError, fetch

logger = logging.getLogger(__name__)

ORIGIN = "codal_search"

_PARAMS = {
    "Audit": "true", "AuditorRef": -1, "Category": -1, "Childs": "true",
    "CompanySearch": "true", "CompanyState": -1, "Isic": -1,
    "IsNotAudited": "false", "Length": -1, "LetterType": -1, "Mains": "true",
    "NotAudited": "true", "NotConsolidated": "true", "Publisher": "false",
    "TracingNo": -1, "search": "true",
}
_HEADERS = {"Referer": "https://codal.ir/", "Origin": "https://codal.ir"}

_SENT_KEY = "codal_search:sent"            # request timestamps, last hour
_RATE_KEY = "codal_search:rate_per_hour"
_PARK_UNTIL_KEY = "codal_search:park_until"
_PARK_SECONDS_KEY = "codal_search:park_seconds"
_LAST_CHANGE_KEY = "codal_search:rate_changed_at"


class CodalSearchThrottled(SourceResponseError):
    """codal.ir refused us (429 or IsAttacker). The origin is now parked."""


# ------------------------------------------------------------------ budget

def _rate(now):
    """The current hourly budget, raised by a step after each clean hour."""
    rate = cache.get(_RATE_KEY) or settings.CODAL_SEARCH_START_PER_HOUR
    changed = cache.get(_LAST_CHANGE_KEY)
    if changed is None:
        cache.set(_LAST_CHANGE_KEY, now, timeout=None)
        return rate
    if now - changed >= 3600 and rate < settings.CODAL_SEARCH_MAX_PER_HOUR:
        rate = min(settings.CODAL_SEARCH_MAX_PER_HOUR, rate + 2)
        cache.set(_RATE_KEY, rate, timeout=None)
        cache.set(_LAST_CHANGE_KEY, now, timeout=None)
    return rate


def _recent(now):
    return [t for t in (cache.get(_SENT_KEY) or []) if now - t < 3600]


def can_send(now=None):
    """Whether one more request fits: not parked, and under the hourly budget."""
    now = time.time() if now is None else now
    if (cache.get(_PARK_UNTIL_KEY) or 0) > now:
        return False
    return len(_recent(now)) < _rate(now)


def _record_sent(now):
    cache.set(_SENT_KEY, _recent(now) + [now], timeout=3600)


def _record_throttled(now):
    """Halve the budget and park, doubling the park on repeat refusals."""
    rate = max(settings.CODAL_SEARCH_MIN_PER_HOUR, (cache.get(_RATE_KEY) or _rate(now)) // 2)
    previous = cache.get(_PARK_SECONDS_KEY) or 0
    park = min(settings.CODAL_SEARCH_MAX_PARK_SECONDS,
               max(settings.CODAL_SEARCH_MIN_PARK_SECONDS, previous * 2))
    cache.set(_RATE_KEY, rate, timeout=None)
    cache.set(_LAST_CHANGE_KEY, now, timeout=None)
    cache.set(_PARK_UNTIL_KEY, now + park, timeout=park)
    cache.set(_PARK_SECONDS_KEY, park, timeout=park * 4)
    logger.warning("codal_search throttled: parked %ss, budget now %s/h", park, rate)


# ------------------------------------------------------------------- fetch

def fetch_letters(day, page):
    """One page of letters published on Jalali `day` ("1404-07-02").

    Returns {"total", "pages", "letters"} with letters normalized by
    `normalize_letter`. Raises CodalSearchThrottled on a refusal and
    SourceResponseError on anything malformed -- never an empty result standing
    in for an error, which would record a busy day as a quiet one.
    """
    now = time.time()
    _record_sent(now)
    slash = day.replace("-", "/")
    try:
        data = fetch(
            settings.CODAL_SEARCH_URL,
            origin=ORIGIN,
            params={**_PARAMS, "FromDate": slash, "ToDate": slash, "PageNumber": page},
            headers=_HEADERS,
            retries=0,
        )
    except SourceResponseError as exc:
        if exc.status_code == 429:
            _record_throttled(now)
            raise CodalSearchThrottled("codal.ir search returned 429", origin=ORIGIN,
                                       status_code=429) from exc
        raise
    if not isinstance(data, dict) or not isinstance(data.get("Letters"), list) \
            or not isinstance(data.get("Total"), int) or not isinstance(data.get("Page"), int):
        raise SourceResponseError("codal.ir search returned an unexpected shape", origin=ORIGIN)
    if data.get("IsAttacker"):
        _record_throttled(now)
        raise CodalSearchThrottled("codal.ir flagged the client (IsAttacker)", origin=ORIGIN)
    cache.delete(_PARK_SECONDS_KEY)
    return {
        "total": data["Total"],
        "pages": data["Page"],
        "letters": [normalize_letter(letter) for letter in data["Letters"]],
    }


def _query_value(url, name):
    match = re.search(rf"[?&]{name}=([^&]+)", url or "", re.I)
    # unquote, never parse_qsl: a literal "+" in a base64 serial must stay "+".
    return unquote(match.group(1)) if match else ""


def _split(stamp):
    date, _, clock = fold_digits(stamp).partition(" ")
    return normalize_jalali(date), clock


def normalize_letter(letter):
    """One API letter -> CodalLetter field values, keeping the raw payload."""
    date_publish, time_publish = _split(letter.get("PublishDateTime"))
    date_sent, time_sent = _split(letter.get("SentDateTime"))
    url = letter.get("Url") or ""
    letter_type = _query_value(url, "let")
    supervision = letter.get("SuperVision") or {}
    return {
        "tracing_no": int(letter["TracingNo"]),
        "letter_serial": _query_value(url, "LetterSerial")
        or _query_value(letter.get("PdfUrl"), "hs"),
        "symbol": (letter.get("Symbol") or "").strip(),
        "company_name": (letter.get("CompanyName") or "").strip(),
        "title": letter.get("Title") or "",
        "letter_code": fold_digits(letter.get("LetterCode")),
        "letter_type": int(letter_type) if letter_type.isdigit() else None,
        "date_publish": date_publish,
        "time_publish": time_publish,
        "date_sent": date_sent,
        "time_sent": time_sent,
        "url": url,
        "pdf_url": letter.get("PdfUrl") or "",
        "excel_url": letter.get("ExcelUrl") or "",
        "xbrl_url": letter.get("XbrlUrl") or "",
        "attachment_url": letter.get("AttachmentUrl") or "",
        "has_html": letter.get("HasHtml"),
        "has_excel": letter.get("HasExcel"),
        "has_pdf": letter.get("HasPdf"),
        "has_xbrl": letter.get("HasXbrl"),
        "has_attachment": letter.get("HasAttachment"),
        "is_estimate": letter.get("IsEstimate"),
        "under_supervision": supervision.get("UnderSupervision"),
        "raw": letter,
    }

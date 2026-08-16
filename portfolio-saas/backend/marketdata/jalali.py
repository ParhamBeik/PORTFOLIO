"""Jalali (Persian) calendar helpers for provider date parameters.

The provider rejects Gregorian dates with HTTP 400 and an explicit message. Since
the archive worker builds date strings programmatically, validating before the
request turns a wasted quota unit into a local error.

`normalize_jalali` is the inbound counterpart: it cleans date strings arriving in
provider payloads. `is_jalali` validates the ones we send out. Both live here so
that `ingest` (which writes) and `validation` (which screens) agree on what a
date is without importing each other.
"""
import datetime
import re
from zoneinfo import ZoneInfo

import jdatetime

JALALI_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_DIGIT_FOLD = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹" "٠١٢٣٤٥٦٧٨٩",  # Persian (U+06F0..) then Arabic-Indic (U+0660..)
    "0123456789" "0123456789",
)


def fold_digits(value) -> str:
    """Return `value` with Persian/Arabic-Indic digits rewritten as ASCII."""
    return str(value or "").strip().translate(_DIGIT_FOLD)


def normalize_jalali(value) -> str:
    """Normalize a date string to ASCII dash-separated Jalali ("1403-10-19").

    If the date is Gregorian (e.g. YYYY-MM-DD or YYYY/MM/DD with year 1900-2100),
    it is parsed and translated to the corresponding Jalali calendar date.
    """
    if not value:
        return ""
    folded = fold_digits(value).replace("/", "-")
    
    # Try detecting and converting standard Gregorian dates
    parts = folded.split("-")
    if len(parts) == 3:
        try:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            if 1900 <= year <= 2100:
                import datetime
                dt = datetime.date(year, month, day)
                jdt = jdatetime.date.fromgregorian(date=dt)
                return jdt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
            
    return folded

# The provider's data starts in 1385; anything outside a sane band is a bug
# (usually a Gregorian year that slipped through).
MIN_YEAR = 1300
MAX_YEAR = 1500


def is_jalali(value):
    if not isinstance(value, str) or not JALALI_DATE_RE.match(value):
        return False
    year, month, day = (int(part) for part in value.split("-"))
    if not MIN_YEAR <= year <= MAX_YEAR:
        return False
    try:
        jdatetime.date(year, month, day)
    except ValueError:
        return False
    return True


def assert_jalali(value, field="date"):
    if not is_jalali(value):
        raise ValueError(
            f"{field}={value!r} is not a valid Jalali date. "
            f"The provider requires Jalali YYYY-MM-DD, e.g. 1404-02-22."
        )
    return value


def today():
    return jdatetime.date.today().strftime("%Y-%m-%d")


def days_ago(days):
    return (jdatetime.date.today() - jdatetime.timedelta(days=days)).strftime("%Y-%m-%d")


def recent_days(count):
    """Return the `count` most recent Jalali dates, newest first.

    Weekends are not filtered here: the provider simply returns an empty list for
    a non-trading day, and the Iranian holiday calendar is not something we can
    derive locally. Callers that care about cost should keep `count` small.
    """
    start = jdatetime.date.today()
    return [
        (start - jdatetime.timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(count)
    ]


TEHRAN = ZoneInfo("Asia/Tehran")


def to_gregorian(value):
    """Jalali "1405-05-24" -> datetime.date, or None if it is not a Jalali date.

    The inverse of what the rest of this module does. Every warehouse table
    stores the provider's Jalali string as its domain key, so this exists for
    the one thing a string cannot be: a range-partition dimension.
    """
    if not is_jalali(value):
        return None
    year, month, day = (int(part) for part in value.split("-"))
    return jdatetime.date(year, month, day).togregorian()


def to_datetime(date_value, time_value=""):
    """Jalali date (+ optional "HH:MM:SS") -> aware UTC datetime, or None.

    Times are read as Tehran local, which is what the provider quotes, and
    returned aware so Postgres stores an unambiguous instant rather than a
    wall-clock reading that shifts twice a year.
    """
    gregorian = to_gregorian(date_value)
    if gregorian is None:
        return None
    parts = fold_digits(time_value).split(":") if time_value else []
    try:
        hour, minute, second = (int(parts[i]) if i < len(parts) else 0 for i in range(3))
    except (TypeError, ValueError):
        hour = minute = second = 0
    return datetime.datetime(
        gregorian.year, gregorian.month, gregorian.day, hour, minute, second,
        tzinfo=TEHRAN,
    ).astimezone(datetime.timezone.utc)

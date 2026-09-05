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
                gregorian = datetime.date(year, month, day)
                return jdatetime.date.fromgregorian(date=gregorian).strftime("%Y-%m-%d")
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
    """The current date on the PROCESS clock, as Jalali. Read the warning below.

    Every container here runs UTC (`TIME_ZONE = "UTC"`, no TZ in the image), so
    this is the UTC date, not the Tehran one. Tehran is UTC+3:30, which means
    that between 20:30 and 23:59 UTC -- 00:00 to 03:29 Tehran -- this returns
    YESTERDAY in Tehran terms. Contrast `from_gregorian`/`from_epoch`/
    `to_datetime` below, which all deliberately read Tehran and say so.

    That gap is currently LOAD-BEARING, which is why it has not simply been
    "fixed". `config/celery.py` declares its crontabs in Tehran (CELERY_TIMEZONE
    = Asia/Tehran), and three nightly jobs land inside exactly that window:
    nightly_asset_metrics (01:00), nightly_asset_signals (01:20) and
    aggregate_market_daily_bars_task (01:40). Each one wants the day that just
    ENDED, and the UTC lag is what hands it to them. Making this Tehran-aware
    without touching those callers would point
    `ingest.aggregate_market_daily_bars` at a Tehran day that is 100 minutes old
    -- it windows on `to_datetime(jalali_date)`, i.e. Tehran midnight -- so the
    day's real bar would be built from almost no snapshots and the previous
    complete day would never be aggregated at all.

    So: if you need "the day that just ended", this is it, but say so at the
    call site. If you need the actual Tehran date, use
    `from_gregorian(market_state._now_tehran())`, and change the three nightly
    callers in the same commit.
    """
    return jdatetime.date.today().strftime("%Y-%m-%d")


def days_ago(days):
    """`days` before `today()`, and therefore on the same UTC clock it is."""
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


def from_gregorian(value):
    """datetime.date (or aware/naive datetime) -> Jalali "1405-05-24".

    The inverse of `to_gregorian`, needed by any source that dates its rows in
    Gregorian or epoch seconds while every warehouse table keys on the Jalali
    string. Wallex's UDF candles are the first such source.

    A datetime is read in TEHRAN, not UTC: a candle stamped 20:30Z belongs to
    the NEXT Tehran day, and dating it by UTC would file every late-session bar
    one day early -- the same off-by-one that put 3.7M duplicate candles in this
    warehouse when `ts` was derived the other way round.
    """
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        value = value.astimezone(TEHRAN).date() if value.tzinfo else value.date()
    jalali = jdatetime.date.fromgregorian(date=value)
    return f"{jalali.year:04d}-{jalali.month:02d}-{jalali.day:02d}"


def from_epoch(seconds):
    """Epoch seconds -> Jalali date string, read in Tehran."""
    if seconds is None:
        return ""
    moment = datetime.datetime.fromtimestamp(int(seconds), tz=datetime.timezone.utc)
    return from_gregorian(moment)


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
